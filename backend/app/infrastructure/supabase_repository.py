"""RLS-scoped PostgREST repository for web accounts.

The publishable key identifies the Supabase project, while every request uses the
current verified user access token. Mutations are routed through audited SQL RPCs;
run capabilities never leave the backend except in those RPC requests.
"""

import json
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Literal, cast
from uuid import UUID

import httpx

from app.application.budgets import BudgetPreset, limits_for_preset
from app.application.errors import ApplicationError
from app.application.model_catalog import profile_for_model
from app.application.web_evidence import WEB_ERROR_MESSAGES
from app.core.config import Settings
from app.core.supabase import project_base_url
from app.domain.context import (
    CheckpointSource,
    ContextCheckpoint,
    ContextCheckpointDraft,
    ContextWindow,
)
from app.domain.event_limits import EVENT_STORAGE_LIMIT_MESSAGE
from app.domain.history import HistoryPage, PageCursor, SessionPage, TimelineRow
from app.domain.models import (
    Message,
    ModelDescriptor,
    ModelId,
    OnboardingProgress,
    OnboardingStep,
    ProviderName,
    Session,
    SessionWorkspaceBusy,
    UserProfile,
)
from app.domain.outputs import AssistantOutput, validate_assistant_batch
from app.domain.recovery import RecoveryContract, RecoveryResolution, RecoverySnapshot
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelContinuationItem,
    ModelMessage,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    RunSummary,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
)
from app.infrastructure.web_repository import WebRepositoryMixin

_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_READ_PAGE_SIZE = 500


class VerifiedTokenSource:
    """Stores only a recent token verified by Supabase Auth for one account."""

    def __init__(self, user_id: UUID) -> None:
        self._user_id = user_id
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def record_verified(self, token: str, verified_user_id: UUID, expires_at: datetime) -> None:
        if verified_user_id != self._user_id:
            raise ValueError("verified token belongs to a different account")
        if (
            not token
            or len(token) > 16_384
            or not token.isascii()
            or any(character.isspace() for character in token)
        ):
            raise ValueError("verified token has an invalid format")
        if expires_at.tzinfo is None:
            raise ValueError("verified token expiry must be timezone aware")
        if expires_at <= datetime.now(UTC):
            raise ValueError("verified token is expired")
        # Multiple tabs can present different still-valid JWTs for this account.
        # A shorter-lived session must not downgrade the token renewing an active run.
        if self._expires_at is None or expires_at >= self._expires_at:
            if self._token != token or self._expires_at != expires_at:
                self._revision += 1
            self._token = token
            self._expires_at = expires_at

    def get_token(self) -> str:
        if self._token is None or self._expires_at is None:
            raise RuntimeError("a verified token is required")
        # Avoid beginning a request so close to expiry that Supabase rejects it in flight.
        if self._expires_at <= datetime.now(UTC) + timedelta(seconds=5):
            raise RuntimeError("verified token has expired")
        return self._token

    def valid_for(self, seconds: int) -> bool:
        return (
            self._token is not None
            and self._expires_at is not None
            and self._expires_at > datetime.now(UTC) + timedelta(seconds=seconds)
        )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError("Supabase returned an invalid object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise RuntimeError("Supabase returned an invalid row list")
    return [_object(item) for item in value]


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"Supabase row has no {key}")
    return value


def _nullable_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"Supabase row has invalid {key}")
    return value


def _integer(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Supabase row has invalid {key}")
    return value


def _boolean(row: Mapping[str, object], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise RuntimeError(f"Supabase row has invalid {key}")
    return value


def _number(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise RuntimeError(f"Supabase row has invalid {key}")
    try:
        return float(value)
    except ValueError:
        raise RuntimeError(f"Supabase row has invalid {key}") from None


def _nullable_integer(row: Mapping[str, object], key: str) -> int | None:
    return None if row.get(key) is None else _integer(row, key)


def _nullable_number(row: Mapping[str, object], key: str) -> float | None:
    return None if row.get(key) is None else _number(row, key)


class SupabaseRepository(WebRepositoryMixin):
    async def load_context_window(self, session_id: str, *, limit: int = 128) -> ContextWindow:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError("context page limit is invalid")
        row = _object(
            await self._request(
                "POST",
                "rpc/read_context_window",
                payload={"p_session_id": session_id, "p_limit": limit},
            )
        )
        checkpoint_row = row.get("checkpoint")
        checkpoint = (
            self._checkpoint(_object(checkpoint_row)) if checkpoint_row is not None else None
        )
        messages = tuple(self._message(message) for message in _array(row.get("messages")))
        if (checkpoint is not None and checkpoint.session_id != session_id) or any(
            message.session_id != session_id for message in messages
        ):
            raise RuntimeError("Supabase returned another chat's context")
        boundary = checkpoint.covered_message_ordinal if checkpoint else 0
        if (
            len(messages) > limit
            or any(message.ordinal <= boundary for message in messages)
            or any(a.ordinal >= b.ordinal for a, b in pairwise(messages))
        ):
            raise RuntimeError("Supabase returned an invalid context window")
        return ContextWindow(checkpoint, messages)

    async def load_context_checkpoint(self, session_id: str) -> ContextCheckpoint | None:
        rows = await self._read(
            "context_checkpoints",
            select="*",
            filters={
                "user_id": f"eq.{self._user_id}",
                "chat_id": f"eq.{session_id}",
                "order": "revision.desc",
                "limit": "1",
            },
        )
        if not rows:
            return None
        checkpoint = self._checkpoint(rows[0])
        if checkpoint.session_id != session_id:
            raise RuntimeError("Supabase returned another chat's checkpoint")
        return checkpoint

    async def save_context_checkpoint(
        self, run_id: str, expected_revision: int, draft: ContextCheckpointDraft
    ) -> ContextCheckpoint:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("checkpoint revision must be a non-negative integer")
        from dataclasses import asdict, replace

        try:
            result = await self._rpc(
                "save_context_checkpoint",
                {**asdict(draft), "run_id": run_id, "expected_revision": expected_revision},
                capability=self._run_capability(run_id),
            )
        except ValueError as error:
            if str(error) == "checkpoint revision conflict":
                raise ApplicationError(
                    "context_checkpoint_conflict",
                    "Context changed while compaction was being saved.",
                ) from None
            raise
        saved = self._checkpoint(_object(result))
        if (
            saved.run_id != run_id
            or replace(saved.draft, pricing_context_tokens=draft.pricing_context_tokens) != draft
            or saved.draft.pricing_context_tokens < draft.pricing_context_tokens
        ):
            raise RuntimeError("Supabase returned another checkpoint result")
        return saved

    async def commit_summary(
        self,
        run_id: str,
        model_call_id: str,
        completion: dict[str, object],
        expected_revision: int,
        draft: ContextCheckpointDraft,
    ) -> tuple[ModelCallRecord, ContextCheckpoint, tuple[RunEvent, ...]]:
        if draft.model_call_id != model_call_id:
            raise ValueError("summary checkpoint belongs to another model call")
        saved_completion = dict(completion)
        if "cached_tokens" in saved_completion:
            saved_completion["cached_read_tokens"] = saved_completion.pop("cached_tokens")
        result = _object(
            await self._idempotent_rpc(
                "commit_summary",
                {
                    "run_id": run_id,
                    "call_id": model_call_id,
                    "completion": saved_completion,
                    "expected_revision": expected_revision,
                    "draft": asdict(draft),
                },
                capability=self._run_capability(run_id),
            )
        )
        call = self._model_call(_object(result.get("model_call")))
        checkpoint = self._checkpoint(_object(result.get("checkpoint")))
        events = tuple(self._event(row) for row in _array(result.get("events")))
        if (
            call.id != model_call_id
            or call.run_id != run_id
            or checkpoint.run_id != run_id
            or checkpoint.id != draft.id
            or checkpoint.revision != expected_revision + 1
            or replace(checkpoint.draft, pricing_context_tokens=draft.pricing_context_tokens)
            != draft
            or checkpoint.draft.pricing_context_tokens < draft.pricing_context_tokens
            or any(item.run_id != run_id for item in events)
        ):
            raise RuntimeError("Supabase returned another summary's completion")
        return call, checkpoint, events

    async def list_context_messages(
        self, session_id: str, after_ordinal: int, *, limit: int = 128
    ) -> list[Message]:
        if after_ordinal < 0 or not 1 <= limit <= 128:
            raise ValueError("context cursor or page limit is invalid")
        checkpoint = await self.load_context_checkpoint(session_id)
        boundary = max(after_ordinal, checkpoint.covered_message_ordinal if checkpoint else 0)
        messages = await self._messages(
            {"chat_id": f"eq.{session_id}", "ordinal": f"gt.{boundary}", "limit": str(limit)}
        )
        if any(message.session_id != session_id for message in messages):
            raise RuntimeError("Supabase returned another chat's context")
        return messages

    def _checkpoint(self, row: dict[str, object]) -> ContextCheckpoint:
        self._owned(row)
        sources = _array(row.get("sources"))
        native_kind = _nullable_text(row, "native_kind")
        if native_kind not in {None, "openai_window", "anthropic_block"}:
            raise RuntimeError("Supabase returned invalid native checkpoint metadata")
        refs: list[CheckpointSource] = []
        for source in sources:
            kind = _text(source, "source_kind")
            if kind not in {"message", "run_message", "checkpoint"}:
                raise RuntimeError("Supabase returned an invalid checkpoint source")
            refs.append(
                CheckpointSource(
                    _text(source, "source_id"),
                    kind,
                    _text(source, "turn_id"),
                )
            )
        draft = ContextCheckpointDraft(
            _text(row, "id"),
            _text(row, "model_call_id"),
            tuple(refs),
            _text(row, "summary"),
            _text(row, "source_hash"),
            _text(row, "input_hash"),
            _text(row, "instruction_hash"),
            _text(row, "workspace_hash"),
            _integer(row, "input_tokens_before"),
            _integer(row, "input_tokens_after"),
            native_kind,
            _nullable_text(row, "native_payload_json"),
            _integer(row, "pricing_context_tokens"),
        )
        return ContextCheckpoint(
            _text(row, "chat_id"),
            _text(row, "run_id"),
            _integer(row, "revision"),
            _nullable_text(row, "parent_id"),
            _text(row, "provider_id"),
            _text(row, "model_id"),
            _text(row, "adapter_kind"),
            _integer(row, "covered_message_ordinal"),
            _integer(row, "covered_run_message_ordinal"),
            _text(row, "created_at"),
            draft,
        )

    def __init__(
        self,
        settings: Settings,
        user_id: UUID,
        token_source: VerifiedTokenSource,
        client: httpx.AsyncClient,
    ) -> None:
        base_url = project_base_url(settings)
        if base_url is None:
            raise RuntimeError("Supabase project URL or publishable key is not configured")
        self._base_url = base_url
        self._publishable_key = cast(str, settings.supabase_publishable_key)
        self._user_id = user_id
        self._token_source = token_source
        self._client = client
        self._capabilities: dict[str, str] = {}
        self._lease_expires: dict[str, datetime] = {}
        self._model_call_runs: dict[str, str] = {}
        self._turn_capabilities: dict[tuple[str, str], str] = {}

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        token = self._token_source.get_token()
        headers = {
            "apikey": self._publishable_key,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if method in {"GET", "HEAD"}:
            headers["Accept-Profile"] = "trellis"
        else:
            headers["Content-Profile"] = "trellis"
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}/rest/v1/{path}",
                headers=headers,
                params=params,
                json=payload,
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.RequestError, TimeoutError:
            raise ApplicationError(
                "database_unavailable", "Cloud data is temporarily unavailable."
            ) from None
        error: dict[str, object] = {}
        if response.status_code >= 400:
            with suppress(ValueError, RuntimeError):
                error = _object(response.json())
        error_code = error.get("code")
        error_message = error.get("message")
        if (
            path in {"rpc/web_mutate", "rpc/web_reserve_search_attempt"}
            and response.status_code >= 400
            and isinstance(error_message, str)
            and error_message in WEB_ERROR_MESSAGES
        ):
            raise ApplicationError(error_message, WEB_ERROR_MESSAGES[error_message])
        if response.status_code in {401, 403}:
            raise ApplicationError("cloud_access_denied", "Your cloud session has expired.")
        if response.status_code == 406 and error_code == "PGRST106":
            raise ApplicationError(
                "database_not_configured",
                "Cloud data needs setup. Expose the trellis schema in Supabase API settings, "
                "then try again.",
            )
        if response.status_code == 409:
            if error_code == "PT409" and error_message == "session_workspace_busy":
                raise SessionWorkspaceBusy
            if error_code == "PT409" and error_message == "session_model_busy":
                raise ApplicationError(
                    "session_model_busy", "Wait for this run to finish before changing its model."
                )
            if error_code == "PT409" and error_message == "session_run_busy":
                raise ApplicationError(
                    "session_run_busy", "This chat has an active run. Try again after it finishes."
                )
            if error_code == "PT409" and error_message == "session_turn_busy":
                raise ApplicationError(
                    "session_turn_busy",
                    "This chat has an active turn. Try again after it finishes.",
                )
            raise ValueError("The cloud record conflicts with the current state")
        if response.status_code == 400 and error_code == "22023":
            if error_message == "event_storage_limit":
                raise ApplicationError("event_storage_limit", EVENT_STORAGE_LIMIT_MESSAGE)
            if isinstance(error_message, str) and len(error_message) <= 200:
                raise ValueError(error_message)
            raise ValueError("The cloud operation is invalid")
        if not 200 <= response.status_code < 300:
            raise ApplicationError("database_unavailable", "Cloud data is temporarily unavailable.")
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        response = await self._send(method, path, params=params, payload=payload)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("Supabase returned invalid JSON") from None

    async def _read(
        self, table: str, *, select: str, filters: dict[str, str] | None = None
    ) -> list[dict[str, object]]:
        params = {"select": select}
        if filters:
            params.update(filters)
        if "limit" in params:
            return _array(await self._request("GET", table, params=params))
        rows: list[dict[str, object]] = []
        offset = int(params.get("offset", "0"))
        while True:
            page = _array(
                await self._request(
                    "GET",
                    table,
                    params={**params, "limit": str(_READ_PAGE_SIZE), "offset": str(offset)},
                )
            )
            rows.extend(page)
            if len(page) < _READ_PAGE_SIZE:
                return rows
            offset += len(page)

    async def _rpc(
        self,
        action: str,
        payload: dict[str, object],
        *,
        capability: str | None = None,
    ) -> object:
        return await self._request(
            "POST",
            "rpc/mutate",
            payload={"p_action": action, "p_payload": payload, "p_capability": capability},
        )

    async def _idempotent_rpc(
        self, action: str, payload: dict[str, object], *, capability: str | None
    ) -> object:
        for attempt in range(2):
            try:
                return await self._rpc(action, payload, capability=capability)
            except ApplicationError as error:
                if attempt or error.code != "database_unavailable":
                    raise
            except RuntimeError as error:
                if attempt or str(error) != "Supabase returned invalid JSON":
                    raise
        raise AssertionError("bounded commit retry exhausted")

    def _owned(self, row: dict[str, object], key: str = "user_id") -> dict[str, object]:
        if _text(row, key) != str(self._user_id):
            raise RuntimeError("Supabase returned another account's row")
        return row

    def _profile(self, row: dict[str, object]) -> UserProfile:
        self._owned(row, "id")
        return UserProfile(
            id=_text(row, "id"),
            display_name=_nullable_text(row, "display_name"),
            email=None,
            created_at=_text(row, "created_at"),
            updated_at=_text(row, "updated_at"),
        )

    async def get_profile(self) -> UserProfile:
        rows = await self._read(
            "profiles",
            select="id,display_name,created_at,updated_at",
            filters={"id": f"eq.{self._user_id}", "limit": "1"},
        )
        if len(rows) != 1:
            raise RuntimeError("Trellis profile has not been initialized")
        return self._profile(rows[0])

    async def update_profile(self, display_name: str | None, email: str | None) -> UserProfile:
        del email  # Sign-in email is exclusively managed by Supabase Auth.
        return self._profile(
            _object(await self._rpc("update_profile", {"display_name": display_name}))
        )

    def _progress(self, row: dict[str, object]) -> OnboardingProgress:
        self._owned(row)
        step = cast(OnboardingStep, _text(row, "current_step"))
        if step not in {"intro", "profile", "model", "complete"}:
            raise RuntimeError("Supabase returned an unknown onboarding step")
        return OnboardingProgress(current_step=step, completed=step == "complete")

    async def get_onboarding_progress(self) -> OnboardingProgress:
        rows = await self._read(
            "onboarding_progress",
            select="user_id,current_step",
            filters={"user_id": f"eq.{self._user_id}", "limit": "1"},
        )
        if len(rows) != 1:
            raise RuntimeError("Trellis onboarding has not been initialized")
        return self._progress(rows[0])

    async def advance_onboarding_intro(self) -> OnboardingProgress:
        return self._progress(_object(await self._rpc("advance_onboarding_intro", {})))

    async def save_onboarding_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]:
        del email
        result = _object(await self._rpc("save_onboarding_profile", {"display_name": display_name}))
        return self._profile(_object(result.get("profile"))), self._progress(
            _object(result.get("progress"))
        )

    async def complete_onboarding(self, model_id: ModelId) -> bool:
        return await self._rpc("complete_onboarding", {"model_id": model_id}) is True

    async def _settings_row(self) -> dict[str, object]:
        rows = await self._read(
            "user_settings",
            select="user_id,selected_provider,selected_model_id,default_budget_preset",
            filters={"user_id": f"eq.{self._user_id}", "limit": "1"},
        )
        if len(rows) != 1:
            raise RuntimeError("Trellis settings have not been initialized")
        return self._owned(rows[0])

    async def get_selected_provider(self) -> ProviderName:
        return _text(await self._settings_row(), "selected_provider")

    async def set_selected_provider(self, provider: ProviderName) -> ProviderName:
        result = await self._rpc("set_selected_provider", {"provider": provider})
        if not isinstance(result, str):
            raise RuntimeError("Supabase returned an invalid provider")
        return result

    async def get_selected_model_id(self) -> ModelId:
        return _text(await self._settings_row(), "selected_model_id")

    async def list_models(self) -> list[ModelDescriptor]:
        return [model for model in await self._models(ready_only=True) if model.enabled]

    async def list_catalog_models(self) -> list[ModelDescriptor]:
        return await self._models(ready_only=False)

    async def _models(self, *, ready_only: bool) -> list[ModelDescriptor]:
        filters = {"order": "catalog_order.asc,id.asc"}
        if ready_only:
            filters.update({"enabled": "eq.true", "metadata_status": "eq.ready"})
        rows = await self._read(
            "models",
            select=(
                "id,provider_id,provider_name,adapter_kind,upstream_model_id,name,"
                "requires_api_key,supports_streaming,supports_tools,enabled,"
                "trusted_metadata,metadata_status,metadata_verified_at,availability,"
                "availability_checked_at"
            ),
            filters=filters,
        )
        models: list[ModelDescriptor] = []
        for row in rows:
            profile = profile_for_model(_text(row, "id"))
            identity_matches = (
                profile is not None
                and profile.provider_id == row.get("provider_id")
                and profile.upstream_model_id == row.get("upstream_model_id")
                and row.get("adapter_kind") == profile.provider_id
            )
            # Old fixtures have no metadata columns. Production migrations store
            # the exact reviewed profile; a changed document cannot redefine it.
            ready = bool(identity_matches and profile is not None and profile.metadata_ready)
            if "metadata_status" in row:
                ready = bool(
                    ready
                    and profile is not None
                    and row.get("metadata_status") == "ready"
                    and row.get("trusted_metadata") == json.loads(json.dumps(asdict(profile)))
                    and row.get("metadata_verified_at")
                )
            availability = row.get("availability", "unknown")
            if availability not in {"available", "unavailable", "unknown"}:
                raise RuntimeError("Supabase returned invalid model availability")
            models.append(
                ModelDescriptor(
                    id=_text(row, "id"),
                    provider_id=_text(row, "provider_id"),
                    provider_name=_text(row, "provider_name"),
                    adapter_kind=_text(row, "adapter_kind"),
                    upstream_model_id=_text(row, "upstream_model_id"),
                    name=_text(row, "name"),
                    requires_api_key=row.get("requires_api_key") is True,
                    supports_streaming=row.get("supports_streaming") is True,
                    supports_tools=row.get("supports_tools") is True,
                    enabled=row.get("enabled") is True and ready,
                    context_window_tokens=profile.context_window_tokens
                    if ready and profile
                    else None,
                    max_input_tokens=profile.max_input_tokens if ready and profile else None,
                    max_output_tokens=profile.max_output_tokens if ready and profile else None,
                    supports_native_compaction=profile.supports_native_compaction
                    if ready and profile
                    else None,
                    metadata_status="ready" if ready else "pending",
                    pending_reasons=() if ready else ("metadata_unverified",),
                    verification_sources=profile.sources if ready and profile else (),
                    metadata_verified_at=_nullable_text(row, "metadata_verified_at"),
                    availability=cast(Literal["available", "unavailable", "unknown"], availability),
                    availability_checked_at=_nullable_text(row, "availability_checked_at"),
                )
            )
        return models

    async def set_selected_model(self, model_id: ModelId) -> bool:
        return await self._rpc("set_selected_model", {"model_id": model_id}) is True

    async def get_default_budget_preset(self) -> BudgetPreset:
        value = _text(await self._settings_row(), "default_budget_preset")
        if value not in {"conservative", "longer"}:
            raise RuntimeError("Supabase returned an unknown run budget")
        return value

    async def set_default_budget_preset(self, preset: BudgetPreset) -> None:
        if preset not in ("conservative", "longer"):
            raise ValueError("Unknown run budget")
        await self._rpc("set_default_budget_preset", {"preset": preset})

    def _session(self, row: dict[str, object]) -> Session:
        self._owned(row)
        message_count = row.get("message_count")
        if (
            isinstance(message_count, bool)
            or not isinstance(message_count, int)
            or message_count < 0
        ):
            raise RuntimeError("Supabase returned an invalid message count")
        return Session(
            id=_text(row, "id"),
            user_id=_text(row, "user_id"),
            title=_text(row, "title"),
            workspace_path=_nullable_text(row, "workspace_path"),
            created_at=_text(row, "created_at"),
            updated_at=_text(row, "updated_at"),
            message_count=message_count,
            model_id=_nullable_text(row, "model_id"),
        )

    async def get_message(self, session_id: str, message_id: str) -> Message | None:
        rows = await self._messages(
            {"chat_id": f"eq.{session_id}", "id": f"eq.{message_id}", "limit": "1"}
        )
        if rows and (rows[0].session_id != session_id or rows[0].id != message_id):
            raise RuntimeError("Supabase returned another message")
        return rows[0] if rows else None

    @staticmethod
    def _page_payload(before: PageCursor | None, limit: int) -> dict[str, object]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        return {
            "p_limit": limit,
            "p_before_time": before.created_at if before else None,
            "p_before_id": before.id if before else None,
        }

    @staticmethod
    def _history_cursor(row: dict[str, object]) -> str | None:
        cursor = row.get("next_before")
        if cursor is None:
            return None
        raw = _object(cursor)
        encoded = PageCursor(
            _text(raw, "created_at"), _text(raw, "kind"), _text(raw, "id")
        ).encode()
        # Reject malformed cursor metadata rather than returning a poisoned page.
        PageCursor.decode(encoded, session_list=raw["kind"] == "session")
        return encoded

    async def read_session_page(self, before: PageCursor | None, *, limit: int = 40) -> SessionPage:
        row = _object(
            await self._request(
                "POST", "rpc/read_chat_summaries_page", payload=self._page_payload(before, limit)
            )
        )
        items = tuple(self._session(item) for item in _array(row.get("items")))
        if len(items) > limit:
            raise RuntimeError("Supabase returned an oversized session page")
        return SessionPage(items, self._history_cursor(row))

    async def read_history_page(
        self, session_id: str, before: PageCursor | None, *, limit: int = 40
    ) -> HistoryPage:
        payload = {
            **self._page_payload(before, limit),
            "p_chat_id": session_id,
            "p_before_kind": before.kind if before else None,
        }
        row = _object(await self._request("POST", "rpc/read_chat_history", payload=payload))
        messages = tuple(self._message(item) for item in _array(row.get("messages")))
        outputs = tuple(
            self._assistant_output(item) for item in _array(row.get("assistant_outputs"))
        )
        order = []
        for item in _array(row.get("timeline_order")):
            kind = _text(item, "kind")
            if kind not in {"message", "assistant_output"}:
                raise RuntimeError("Supabase returned an invalid timeline kind")
            order.append(TimelineRow(kind, _text(item, "id")))
        expected = {("message", item.id) for item in messages} | {
            ("assistant_output", item.id) for item in outputs if item.promoted_message_id is None
        }
        if (
            len(order) > limit
            or len(messages) > limit
            or len(outputs) > limit
            or len(order) != len(expected)
            or {(item.kind, item.id) for item in order} != expected
            or any(item.session_id != session_id for item in (*messages, *outputs))
        ):
            raise RuntimeError("Supabase returned an invalid history page")
        return HistoryPage(messages, outputs, tuple(order), self._history_cursor(row))

    async def list_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        offset = 0
        while True:
            rows = _array(
                await self._request(
                    "POST",
                    "rpc/list_chat_summaries",
                    params={
                        "limit": str(_READ_PAGE_SIZE),
                        "offset": str(offset),
                        "order": "updated_at.desc,id.desc",
                    },
                    payload={},
                )
            )
            sessions.extend(self._session(row) for row in rows)
            if len(rows) < _READ_PAGE_SIZE:
                return sessions
            offset += len(rows)

    async def create_session(self, workspace_path: str | None = None) -> Session:
        result = self._owned(
            _object(await self._rpc("create_session", {"workspace_path": workspace_path}))
        )
        session = await self.get_session(_text(result, "id"))
        if session is None:
            raise RuntimeError("Created session could not be loaded")
        return session

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> Session | None:
        result = await self._rpc(
            "set_session_workspace", {"session_id": session_id, "workspace_path": workspace_path}
        )
        if result is None:
            return None
        self._owned(_object(result))
        return await self.get_session(session_id)

    async def get_session(self, session_id: str) -> Session | None:
        rows = _array(
            await self._request(
                "POST",
                "rpc/list_chat_summaries",
                params={"limit": "1"},
                payload={"p_chat_id": session_id},
            )
        )
        return self._session(rows[0]) if rows else None

    async def set_session_model(self, session_id: str, model_id: str) -> Session | None:
        result = await self._rpc(
            "set_session_model", {"session_id": session_id, "model_id": model_id}
        )
        if result is None:
            return None
        self._owned(_object(result))
        return await self.get_session(session_id)

    def _message(self, row: dict[str, object]) -> Message:
        self._owned(row)
        role = _text(row, "role")
        if role not in {"user", "assistant"}:
            raise RuntimeError("Supabase returned an invalid message role")
        return Message(
            id=_text(row, "id"),
            session_id=_text(row, "chat_id"),
            turn_id=_text(row, "turn_id"),
            ordinal=_integer(row, "ordinal"),
            role=role,
            content=_text(row, "content"),
            provider=_nullable_text(row, "provider"),
            model=_nullable_text(row, "model"),
            created_at=_text(row, "created_at"),
        )

    async def _messages(self, filters: dict[str, str]) -> list[Message]:
        rows = await self._read(
            "messages",
            select="id,user_id,chat_id,turn_id,ordinal,role,content,provider,model,created_at",
            filters={"user_id": f"eq.{self._user_id}", **filters, "order": "ordinal.asc"},
        )
        return [self._message(row) for row in rows]

    async def list_messages(self, session_id: str) -> list[Message]:
        return await self._messages({"chat_id": f"eq.{session_id}"})

    async def get_turn_messages(self, session_id: str, turn_id: str) -> list[Message]:
        return await self._messages({"chat_id": f"eq.{session_id}", "turn_id": f"eq.{turn_id}"})

    async def claim_turn(self, session_id: str, turn_id: str) -> bool:
        capability = secrets.token_urlsafe(48)
        claimed = (
            await self._rpc(
                "claim_turn",
                {"session_id": session_id, "turn_id": turn_id},
                capability=capability,
            )
            is True
        )
        if claimed:
            self._turn_capabilities[(session_id, turn_id)] = capability
        return claimed

    def _turn_capability(self, session_id: str, turn_id: str) -> str:
        capability = self._turn_capabilities.get((session_id, turn_id))
        if capability is None:
            raise ApplicationError("turn_not_owned", "This turn belongs to another device.")
        return capability

    async def release_turn(self, session_id: str, turn_id: str) -> None:
        await self._rpc(
            "release_turn",
            {"session_id": session_id, "turn_id": turn_id},
            capability=self._turn_capability(session_id, turn_id),
        )
        self._turn_capabilities.pop((session_id, turn_id), None)

    async def add_user_message(self, session_id: str, turn_id: str, content: str) -> Message:
        result = await self._rpc(
            "add_user_message",
            {"session_id": session_id, "turn_id": turn_id, "content": content},
            capability=self._turn_capability(session_id, turn_id),
        )
        return self._message(_object(result))

    async def add_assistant_message(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        provider: ProviderName,
        model: str,
    ) -> Message:
        result = await self._rpc(
            "add_assistant_message",
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "content": content,
                "provider": provider,
                "model": model,
            },
            capability=self._turn_capability(session_id, turn_id),
        )
        return self._message(_object(result))

    def _run(self, row: dict[str, object]) -> RunSnapshot:
        self._owned(row)
        return RunSnapshot(
            id=_text(row, "id"),
            session_id=_text(row, "chat_id"),
            turn_id=_text(row, "turn_id"),
            status=RunStatus(_text(row, "status")),
            provider_id=_text(row, "provider_id"),
            model_id=_text(row, "model_id"),
            adapter_kind=_text(row, "adapter_kind"),
            upstream_model_id=_text(row, "upstream_model_id"),
            input_message_id=_text(row, "input_message_id"),
            retry_of=_nullable_text(row, "retry_of"),
            client_request_id=_text(row, "client_request_id"),
            max_model_calls=_integer(row, "max_model_calls"),
            max_tool_calls=_integer(row, "max_tool_calls"),
            budget_preset=_text(row, "budget_preset"),
            max_total_tokens=_integer(row, "max_total_tokens"),
            max_cost_usd=_number(row, "max_cost_usd"),
            deadline_at=_text(row, "deadline_at"),
            cancel_requested_at=_nullable_text(row, "cancel_requested_at"),
            lease_expires_at=_nullable_text(row, "lease_expires_at"),
            recovery_count=_integer(row, "recovery_count"),
            stop_reason=_nullable_text(row, "stop_reason"),
            last_event_sequence=_integer(row, "last_event_sequence"),
            error_code=_nullable_text(row, "error_code"),
            error_message=_nullable_text(row, "error_message"),
            created_at=_text(row, "created_at"),
            started_at=_nullable_text(row, "started_at"),
            finished_at=_nullable_text(row, "finished_at"),
            recovery_contract=(
                RecoveryContract.from_dict(_object(row["recovery_contract"]))
                if row.get("recovery_contract") is not None
                else None
            ),
            generation=_integer(row, "generation") if "generation" in row else 0,
            retry_allowed=_boolean(row, "retry_allowed") if "retry_allowed" in row else True,
            recovery_reason=_nullable_text(row, "recovery_reason"),
            uncertain_tool_call_id=_nullable_text(row, "uncertain_tool_call_id"),
        )

    def _event(self, row: dict[str, object]) -> RunEvent:
        self._owned(row)
        return RunEvent(
            run_id=_text(row, "run_id"),
            sequence=_integer(row, "sequence"),
            event_type=RunEventType(_text(row, "event_type")),
            event_version=_integer(row, "event_version"),
            data=_object(row.get("data")),
            created_at=_text(row, "created_at"),
        )

    def _run_message(
        self,
        row: dict[str, object],
        tool_calls: tuple[ModelToolCall, ...] = (),
    ) -> RunMessageRecord:
        self._owned(row)
        raw_items = row.get("continuation")
        if not isinstance(raw_items, list):
            raise RuntimeError("Supabase returned invalid run continuation")
        continuation_items = tuple(
            ModelContinuationItem(
                provider_id=_text(_object(item), "provider_id"),
                model_id=_text(_object(item), "model_id"),
                payload_json=_text(_object(item), "payload_json"),
            )
            for item in raw_items
        )
        role = _text(row, "role")
        if role not in {"assistant", "tool"}:
            raise RuntimeError("Supabase returned invalid run message role")
        return RunMessageRecord(
            id=_text(row, "id"),
            run_id=_text(row, "run_id"),
            ordinal=_integer(row, "ordinal"),
            role=role,
            content=_text(row, "content"),
            model_call_id=_nullable_text(row, "model_call_id"),
            tool_call_id=_nullable_text(row, "tool_call_id"),
            created_at=_text(row, "created_at"),
            tool_calls=tool_calls,
            continuation_items=continuation_items,
        )

    async def _runs(self, filters: dict[str, str]) -> list[RunSnapshot]:
        rows = await self._read(
            "runs",
            select=(
                "id,user_id,chat_id,turn_id,status,provider_id,model_id,adapter_kind,"
                "upstream_model_id,input_message_id,retry_of,client_request_id,"
                "max_model_calls,max_tool_calls,budget_preset,max_total_tokens,max_cost_usd,"
                "deadline_at,cancel_requested_at,lease_expires_at,recovery_count,stop_reason,"
                "last_event_sequence,error_code,error_message,created_at,started_at,finished_at,"
                "waiting_tool_call_id,recovery_contract,generation,retry_allowed,"
                "recovery_reason,uncertain_tool_call_id"
            ),
            filters={"user_id": f"eq.{self._user_id}", **filters},
        )
        return [self._run(row) for row in rows]

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        rows = await self._runs({"id": f"eq.{run_id}", "limit": "1"})
        return rows[0] if rows else None

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None:
        rows = await self._runs(
            {"chat_id": f"eq.{session_id}", "order": "created_at.desc,id.desc", "limit": "1"}
        )
        return rows[0] if rows else None

    async def list_run_summaries_for_session(
        self,
        session_id: str,
        offset: int,
        limit: int,
        *,
        turn_id: str | None = None,
        newest_first: bool = False,
    ) -> list[RunSummary]:
        if offset < 0 or not 1 <= limit <= 101:
            raise ValueError("run offset and limit are outside the supported range")
        rows = _array(
            await self._request(
                "POST",
                "rpc/read_session_run_summaries",
                payload={
                    "p_chat_id": session_id,
                    "p_offset": offset,
                    "p_limit": limit,
                    "p_turn_id": turn_id,
                    "p_newest_first": newest_first,
                },
            )
        )
        summaries: list[RunSummary] = []
        for row in rows:
            run = self._run(_object(row.get("run")))
            if run.session_id != session_id or (turn_id is not None and run.turn_id != turn_id):
                raise RuntimeError("Supabase returned another session's run summary")
            summaries.append(
                RunSummary(
                    run=run,
                    model_call_count=_integer(row, "model_call_count"),
                    tool_call_count=_integer(row, "tool_call_count"),
                    total_tokens=_nullable_integer(row, "total_tokens"),
                    usage_complete=_boolean(row, "usage_complete"),
                    estimated_cost_usd=_nullable_number(row, "estimated_cost_usd"),
                    cost_complete=_boolean(row, "cost_complete"),
                )
            )
        return summaries

    async def list_run_events(
        self, run_id: str, after_sequence: int = 0, *, limit: int = 500
    ) -> list[RunEvent]:
        if after_sequence < 0 or not 1 <= limit <= 501:
            raise ValueError("event cursor and limit are outside the supported range")
        rows = await self._read(
            "run_events",
            select="user_id,run_id,sequence,event_type,event_version,data,created_at",
            filters={
                "user_id": f"eq.{self._user_id}",
                "run_id": f"eq.{run_id}",
                "sequence": f"gt.{after_sequence}",
                "order": "sequence.asc",
                "limit": str(limit),
            },
        )
        return [self._event(row) for row in rows]

    def _model_call(self, row: dict[str, object]) -> ModelCallRecord:
        self._owned(row)
        snapshot = row.get("response_snapshot")
        return ModelCallRecord(
            id=_text(row, "id"),
            run_id=_text(row, "run_id"),
            step_index=_integer(row, "step_index"),
            provider_id=_text(row, "provider_id"),
            model_id=_text(row, "model_id"),
            adapter_kind=_text(row, "adapter_kind"),
            status=ModelCallStatus(_text(row, "status")),
            request_snapshot=_object(row.get("request_snapshot")),
            response_snapshot=None if snapshot is None else _object(snapshot),
            provider_response_id=_nullable_text(row, "provider_response_id"),
            finish_reason=_nullable_text(row, "finish_reason"),
            input_tokens=_nullable_integer(row, "input_tokens"),
            output_tokens=_nullable_integer(row, "output_tokens"),
            reasoning_tokens=_nullable_integer(row, "reasoning_tokens"),
            cached_tokens=_nullable_integer(row, "cached_read_tokens"),
            cache_creation_tokens=_nullable_integer(row, "cache_creation_tokens"),
            estimated_cost=_nullable_number(row, "estimated_cost"),
            error_code=_nullable_text(row, "error_code"),
            error_message=_nullable_text(row, "error_message"),
            started_at=_text(row, "started_at"),
            finished_at=_nullable_text(row, "finished_at"),
            accounted_total_tokens=_nullable_integer(row, "accounted_total_tokens"),
        )

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        rows = await self._read(
            "model_calls",
            select=(
                "id,user_id,run_id,step_index,provider_id,model_id,adapter_kind,status,"
                "request_snapshot,response_snapshot,provider_response_id,finish_reason,"
                "input_tokens,output_tokens,reasoning_tokens,cached_read_tokens,"
                "cache_creation_tokens,estimated_cost,error_code,error_message,started_at,"
                "finished_at,accounted_total_tokens"
            ),
            filters={
                "user_id": f"eq.{self._user_id}",
                "run_id": f"eq.{run_id}",
                "order": "step_index.asc",
            },
        )
        calls = [self._model_call(row) for row in rows]
        self._model_call_runs.update({call.id: call.run_id for call in calls})
        return calls

    def _tool_call(self, row: dict[str, object]) -> ToolCallRecord:
        self._owned(row)
        decision = _nullable_text(row, "approval_decision")
        preview = row.get("approval_preview")
        input_snapshot = row.get("input_snapshot")
        return ToolCallRecord(
            id=_text(row, "id"),
            run_id=_text(row, "run_id"),
            assistant_message_id=_text(row, "assistant_message_id"),
            call_index=_integer(row, "call_index"),
            provider_call_id=_text(row, "provider_call_id"),
            name=_text(row, "name"),
            arguments=_object(row.get("arguments")),
            status=ToolCallStatus(_text(row, "status")),
            approval_decision=None if decision is None else ToolApprovalDecision(decision),
            approval_decided_at=_nullable_text(row, "approval_decided_at"),
            approval_preview=None if preview is None else _object(preview),
            created_at=_text(row, "created_at"),
            finished_at=_nullable_text(row, "finished_at"),
            dispatch_attempt=_integer(row, "dispatch_attempt") if "dispatch_attempt" in row else 0,
            dispatch_generation=_nullable_integer(row, "dispatch_generation"),
            dispatched_at=_nullable_text(row, "dispatched_at"),
            recovery_policy=_nullable_text(row, "recovery_policy"),
            result_source=_nullable_text(row, "result_source"),
            dispatch_id=_nullable_text(row, "dispatch_id"),
            input_snapshot=None if input_snapshot is None else _object(input_snapshot),
            reviewed_at=_nullable_text(row, "reviewed_at"),
            result_snapshot=(
                None if row.get("result_snapshot") is None else _object(row["result_snapshot"])
            ),
        )

    async def _tool_call_rows(self, run_id: str) -> list[dict[str, object]]:
        return await self._read(
            "tool_calls",
            select=(
                "id,user_id,run_id,assistant_message_id,call_index,provider_call_id,name,"
                "arguments,status,approval_decision,approval_decided_at,approval_preview,"
                "created_at,finished_at,dispatch_attempt,dispatch_generation,dispatched_at,"
                "recovery_policy,result_source,dispatch_id,input_snapshot,reviewed_at,result_snapshot"
            ),
            filters={
                "user_id": f"eq.{self._user_id}",
                "run_id": f"eq.{run_id}",
                "order": "call_index.asc",
            },
        )

    async def _run_message_rows(self, run_id: str) -> list[dict[str, object]]:
        return await self._read(
            "run_messages",
            select=(
                "id,user_id,run_id,ordinal,role,content,continuation,model_call_id,"
                "tool_call_id,created_at"
            ),
            filters={
                "user_id": f"eq.{self._user_id}",
                "run_id": f"eq.{run_id}",
                "order": "ordinal.asc",
            },
        )

    async def list_run_messages(self, run_id: str) -> list[RunMessageRecord]:
        rows = await self._run_message_rows(run_id)
        calls = [self._tool_call(row) for row in await self._tool_call_rows(run_id)]
        by_message: dict[str, list[ModelToolCall]] = {}
        for call in calls:
            by_message.setdefault(call.assistant_message_id, []).append(
                ModelToolCall(call.provider_call_id, call.name, call.arguments)
            )
        return [self._run_message(row, tuple(by_message.get(_text(row, "id"), ()))) for row in rows]

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        messages = await self._run_message_rows(run_id)
        order = {_text(row, "id"): _integer(row, "ordinal") for row in messages}
        calls = [self._tool_call(row) for row in await self._tool_call_rows(run_id)]
        return sorted(
            calls, key=lambda call: (order.get(call.assistant_message_id, 0), call.call_index)
        )

    async def list_decided_approval_runs(self) -> list[str]:
        decided: list[str] = []
        for run_id in tuple(self._capabilities):
            if not self.owns_run_lease(run_id):
                continue
            run = await self.get_run(run_id)
            if run is None or run.status is not RunStatus.WAITING_FOR_APPROVAL:
                continue
            calls = await self.list_tool_calls(run_id)
            if any(
                call.status is ToolCallStatus.PENDING and call.approval_decision is not None
                for call in calls
            ):
                decided.append(run_id)
        return decided

    def _run_capability(self, run_id: str) -> str:
        if not self.owns_run_lease(run_id):
            raise ApplicationError("run_not_owned", "This run is active on another computer.")
        try:
            return self._capabilities[run_id]
        except KeyError:
            raise ApplicationError(
                "run_not_owned", "This run is active on another computer."
            ) from None

    def owns_run_lease(self, run_id: str) -> bool:
        expires_at = self._lease_expires.get(run_id)
        if expires_at is None or expires_at <= datetime.now(UTC):
            self._capabilities.pop(run_id, None)
            self._lease_expires.pop(run_id, None)
            return False
        return run_id in self._capabilities

    def _record_lease(self, run: RunSnapshot, capability: str) -> None:
        raw_expiry = run.lease_expires_at
        if raw_expiry is None:
            raise RuntimeError("Supabase granted a run without a lease expiry")
        try:
            expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeError("Supabase returned an invalid lease expiry") from None
        if expires_at.tzinfo is None:
            raise RuntimeError("Supabase returned an invalid lease expiry")
        if expires_at <= datetime.now(UTC):
            return
        self._capabilities[run.id] = capability
        self._lease_expires[run.id] = expires_at

    def _drop_lease(self, run_id: str) -> None:
        self._capabilities.pop(run_id, None)
        self._lease_expires.pop(run_id, None)

    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
        *,
        budget_preset: BudgetPreset = "conservative",
        request_fingerprint: str | None = None,
        submitted_model_id: str | None = None,
        submitted_budget_preset: BudgetPreset | None = None,
        recovery_contract: RecoveryContract | None = None,
    ) -> RunSnapshot:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("run input cannot be empty")
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("client request ID must contain 1 to 200 characters")
        if not self._token_source.valid_for(limits_for_preset(budget_preset).max_seconds + 30):
            raise ApplicationError(
                "run_token_too_short",
                "Your sign-in expires too soon for this run. Refresh your sign-in and try again.",
            )
        capability = secrets.token_urlsafe(32)
        payload: dict[str, object] = {
            "session_id": session_id,
            "turn_id": turn_id,
            "client_request_id": client_request_id,
            "content": normalized_content,
            "model_id": model.id,
            "budget_preset": budget_preset,
            "request_fingerprint": request_fingerprint,
            "submitted_model_id": submitted_model_id,
            "submitted_budget_preset": submitted_budget_preset,
        }
        if recovery_contract is not None:
            if recovery_contract.request_identity != client_request_id:
                raise ValueError("Recovery contract belongs to another request")
            payload["recovery_contract"] = recovery_contract.to_dict()
        for attempt in range(2):
            try:
                result = _object(await self._rpc("create_run", payload, capability=capability))
                break
            except (ApplicationError, RuntimeError) as error:
                if (
                    isinstance(error, ApplicationError) and error.code != "database_unavailable"
                ) or (
                    isinstance(error, RuntimeError)
                    and str(error) != "Supabase returned invalid JSON"
                ):
                    raise
                accepted = await self._rpc("match_run_request", payload, capability=capability)
                if accepted is not None:
                    result = _object(accepted)
                    break
                if attempt:
                    raise
        run = self._run(_object(result.get("run")))
        if result.get("lease_acquired") is True:
            if recovery_contract is not None and run.recovery_contract != recovery_contract:
                raise RuntimeError("Supabase did not preserve the new run's recovery contract")
            self._record_lease(run, capability)
        return run

    async def match_run_request(
        self,
        session_id: str,
        client_request_id: str,
        *,
        turn_id: str,
        content: str,
        request_fingerprint: str,
        submitted_model_id: str | None,
        submitted_budget_preset: BudgetPreset | None,
    ) -> RunSnapshot | None:
        result = await self._rpc(
            "match_run_request",
            {
                "session_id": session_id,
                "client_request_id": client_request_id,
                "turn_id": turn_id,
                "content": content,
                "request_fingerprint": request_fingerprint,
                "submitted_model_id": submitted_model_id,
                "submitted_budget_preset": submitted_budget_preset,
            },
        )
        return self._run(_object(_object(result).get("run"))) if result is not None else None

    async def lookup_run(self, session_id: str, client_request_id: str) -> RunSnapshot | None:
        result = await self._rpc(
            "lookup_run", {"session_id": session_id, "client_request_id": client_request_id}
        )
        return self._run(_object(result)) if result is not None else None

    async def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent:
        result = await self._rpc(
            "append_run_event",
            {
                "run_id": run_id,
                "event_type": event_type.value,
                "data": data,
                "event_version": event_version,
            },
            capability=self._run_capability(run_id),
        )
        return self._event(_object(result))

    async def transition_run_record(
        self,
        run_id: str,
        next_status: RunStatus,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        stop_reason: str | None = None,
    ) -> RunEvent:
        result = await self._rpc(
            "transition_run_record",
            {
                "run_id": run_id,
                "next_status": next_status.value,
                "event_type": event_type.value,
                "data": data,
                "error_code": error_code,
                "error_message": error_message,
                "stop_reason": stop_reason,
            },
            capability=self._run_capability(run_id),
        )
        event = self._event(_object(result))
        if next_status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            self._capabilities.pop(run_id, None)
            self._lease_expires.pop(run_id, None)
        return event

    async def complete_run(self, run_id: str, content: str) -> tuple[Message, tuple[RunEvent, ...]]:
        if not content.strip():
            raise ValueError("assistant response cannot be empty")
        result = _object(
            await self._rpc(
                "complete_run",
                {"run_id": run_id, "content": content},
                capability=self._run_capability(run_id),
            )
        )
        events = tuple(self._event(row) for row in _array(result.get("events")))
        message = self._message(_object(result.get("message")))
        self._capabilities.pop(run_id, None)
        self._lease_expires.pop(run_id, None)
        return message, events

    async def create_model_call(
        self,
        run_id: str,
        step_index: int,
        model: ModelDescriptor,
        request_snapshot: dict[str, object],
    ) -> ModelCallRecord:
        if step_index < 1:
            raise ValueError("model call step index must be positive")
        result = await self._idempotent_rpc(
            "create_model_call",
            {
                "run_id": run_id,
                "step_index": step_index,
                "model_id": model.id,
                "request_snapshot": request_snapshot,
            },
            capability=self._run_capability(run_id),
        )
        call = self._model_call(_object(result))
        if (
            call.run_id != run_id
            or call.step_index != step_index
            or call.model_id != model.id
            or call.request_snapshot != request_snapshot
        ):
            raise RuntimeError("Supabase returned a model call from another run")
        self._model_call_runs[call.id] = run_id
        return call

    async def update_model_call(
        self,
        call_id: str,
        next_status: ModelCallStatus,
        *,
        response_snapshot: dict[str, object] | None = None,
        provider_response_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        estimated_cost: float | None = None,
        clear_estimated_cost: bool = False,
        accounted_total_tokens: int | None = None,
        clear_accounted_total_tokens: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelCallRecord:
        run_id = self._model_call_runs.get(call_id)
        if run_id is None:
            raise ValueError("model call has no local run lease")
        result = await self._rpc(
            "update_model_call",
            {
                "call_id": call_id,
                "next_status": next_status.value,
                "response_snapshot": response_snapshot,
                "provider_response_id": provider_response_id,
                "finish_reason": finish_reason,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_read_tokens": cached_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "estimated_cost": estimated_cost,
                "clear_estimated_cost": clear_estimated_cost,
                "accounted_total_tokens": accounted_total_tokens,
                "clear_accounted_total_tokens": clear_accounted_total_tokens,
                "error_code": error_code,
                "error_message": error_message,
            },
            capability=self._run_capability(run_id),
        )
        call = self._model_call(_object(result))
        if call.id != call_id or call.run_id != run_id:
            raise RuntimeError("Supabase returned an unexpected model call")
        return call

    async def record_assistant_message(
        self,
        run_id: str,
        model_call_id: str,
        message: ModelMessage,
    ) -> tuple[RunMessageRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
        if message.role != "assistant" or message.tool_call_id is not None:
            raise ValueError("an assistant run message is required")
        if not message.content.strip() and not message.tool_calls:
            raise ValueError("assistant run message cannot be empty")
        provider_ids = [call.id for call in message.tool_calls]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("tool call IDs must be unique within an assistant message")
        continuation = [
            {
                "provider_id": item.provider_id,
                "model_id": item.model_id,
                "payload_json": item.payload_json,
            }
            for item in message.continuation_items
        ]
        if len(json.dumps(continuation, allow_nan=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("assistant continuation exceeds the storage limit")
        result = _object(
            await self._rpc(
                "record_assistant_message",
                {
                    "run_id": run_id,
                    "model_call_id": model_call_id,
                    "message": {
                        "role": message.role,
                        "content": message.content,
                        "tool_call_id": message.tool_call_id,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in message.tool_calls
                        ],
                        "continuation_items": continuation,
                    },
                },
                capability=self._run_capability(run_id),
            )
        )
        assistant = self._run_message(_object(result.get("message")), message.tool_calls)
        calls = tuple(self._tool_call(row) for row in _array(result.get("tool_calls")))
        events = tuple(self._event(row) for row in _array(result.get("events")))
        return assistant, calls, events

    async def commit_generation(
        self,
        run_id: str,
        call_id: str,
        completion: dict[str, object],
        *,
        assistant_message: ModelMessage | None,
        final_content: str | None,
    ) -> tuple[ModelCallRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
        if (assistant_message is None) == (final_content is None):
            raise ValueError("generation requires exactly one completion outcome")
        saved_completion = dict(completion)
        if "cached_tokens" in saved_completion:
            saved_completion["cached_read_tokens"] = saved_completion.pop("cached_tokens")
        message: dict[str, object] | None = None
        if assistant_message is not None:
            if assistant_message.role != "assistant" or assistant_message.tool_call_id is not None:
                raise ValueError("generation requires an assistant message")
            message = {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_call_id": None,
                "tool_calls": [asdict(item) for item in assistant_message.tool_calls],
                "continuation_items": [
                    asdict(item) for item in assistant_message.continuation_items
                ],
            }
        result = _object(
            await self._idempotent_rpc(
                "commit_generation",
                {
                    "run_id": run_id,
                    "call_id": call_id,
                    "completion": saved_completion,
                    "assistant_message": message,
                    "final_content": final_content,
                },
                capability=self._run_capability(run_id),
            )
        )
        call = self._model_call(_object(result.get("model_call")))
        tools = tuple(self._tool_call(row) for row in _array(result.get("tool_calls")))
        events = tuple(self._event(row) for row in _array(result.get("events")))
        if (
            call.id != call_id
            or call.run_id != run_id
            or any(item.run_id != run_id for item in (*tools, *events))
        ):
            raise RuntimeError("Supabase returned another generation's completion")
        if final_content is not None:
            self._drop_lease(run_id)
        return call, tools, events

    async def commit_native_compaction(
        self,
        run_id: str,
        call_id: str,
        completion: dict[str, object],
        *,
        status: ModelCallStatus,
    ) -> tuple[ModelCallRecord, tuple[RunEvent, ...]]:
        if status not in {ModelCallStatus.COMPLETED, ModelCallStatus.FAILED}:
            raise ValueError("native compaction requires a known completion")
        saved_completion = dict(completion)
        if "cached_tokens" in saved_completion:
            saved_completion["cached_read_tokens"] = saved_completion.pop("cached_tokens")
        result = _object(
            await self._idempotent_rpc(
                "commit_native_compaction",
                {
                    "run_id": run_id,
                    "call_id": call_id,
                    "completion": saved_completion,
                    "next_status": status.value,
                },
                capability=self._run_capability(run_id),
            )
        )
        call = self._model_call(_object(result.get("model_call")))
        events = tuple(self._event(row) for row in _array(result.get("events")))
        if (
            call.id != call_id
            or call.run_id != run_id
            or call.status is not status
            or any(item.run_id != run_id for item in events)
        ):
            raise RuntimeError("Supabase returned another native compaction's completion")
        return call, events

    async def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        content: str,
        *,
        status: ToolCallStatus = ToolCallStatus.COMPLETED,
        result_source: str = "executor",
        result_snapshot: dict[str, object] | None = None,
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]:
        if status not in {
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.CANCELLED,
            ToolCallStatus.TIMED_OUT,
        }:
            raise ValueError("tool result requires a terminal status")
        result = _object(
            await self._idempotent_rpc(
                "record_tool_result",
                {
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "status": status.value,
                    "result_source": result_source,
                    "result_snapshot": result_snapshot,
                },
                capability=self._run_capability(run_id),
            )
        )
        message = self._run_message(_object(result.get("message")))
        tool = self._tool_call(_object(result.get("tool_call")))
        event = self._event(_object(result.get("event")))
        if (
            message.run_id != run_id
            or message.tool_call_id != tool_call_id
            or message.role != "tool"
            or message.content != content
            or tool.run_id != run_id
            or tool.id != tool_call_id
            or tool.status is not status
            or tool.result_snapshot != result_snapshot
            or event.run_id != run_id
        ):
            raise RuntimeError("Supabase returned another tool's result")
        return message, tool, event

    async def begin_tool_execution(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        dispatch_id: str,
        expected_attempt: int,
        recovery_policy: str,
        input_snapshot: dict[str, object] | None = None,
    ) -> tuple[ToolCallRecord, RunEvent]:
        result = _object(
            await self._idempotent_rpc(
                "begin_tool_execution",
                {
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "dispatch_id": dispatch_id,
                    "expected_attempt": expected_attempt,
                    "recovery_policy": recovery_policy,
                    "input_snapshot": input_snapshot,
                },
                capability=self._run_capability(run_id),
            )
        )
        tool = self._tool_call(_object(result.get("tool_call")))
        event = self._event(_object(result.get("event")))
        if (
            tool.id != tool_call_id
            or tool.run_id != run_id
            or tool.dispatch_id != dispatch_id
            or event.run_id != run_id
        ):
            raise RuntimeError("Supabase returned another tool's dispatch")
        return tool, event

    async def mark_tool_outcome_unknown(
        self, run_id: str, tool_call_id: str, reason: str
    ) -> ToolCallRecord:
        result = await self._idempotent_rpc(
            "mark_tool_outcome_unknown",
            {"run_id": run_id, "tool_call_id": tool_call_id, "reason": reason},
            capability=self._run_capability(run_id),
        )
        tool = self._tool_call(_object(result))
        if (
            tool.id != tool_call_id
            or tool.run_id != run_id
            or tool.status is not ToolCallStatus.OUTCOME_UNKNOWN
        ):
            raise RuntimeError("Supabase returned another uncertain tool")
        return tool

    async def acknowledge_workspace_review(
        self, session_id: str, run_id: str, tool_call_id: str
    ) -> ToolCallRecord:
        row = await self._idempotent_rpc(
            "acknowledge_workspace_review",
            {"session_id": session_id, "run_id": run_id, "tool_call_id": tool_call_id},
            capability=None,
        )
        tool = self._tool_call(_object(row))
        if (
            tool.id != tool_call_id
            or tool.run_id != run_id
            or tool.name != "run_command"
            or tool.status is not ToolCallStatus.OUTCOME_UNKNOWN
            or tool.reviewed_at is None
        ):
            raise RuntimeError("Supabase returned another command review acknowledgment")
        return tool

    async def authorize_tool_retry(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        expected_dispatch_id: str,
        expected_attempt: int,
        reconciliation: str,
        proof: dict[str, object] | None = None,
    ) -> ToolCallRecord:
        result = await self._idempotent_rpc(
            "authorize_tool_retry",
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "expected_dispatch_id": expected_dispatch_id,
                "expected_attempt": expected_attempt,
                "reconciliation": reconciliation,
                "proof": proof,
            },
            capability=self._run_capability(run_id),
        )
        tool = self._tool_call(_object(result))
        if (
            tool.id != tool_call_id
            or tool.run_id != run_id
            or tool.dispatch_id != expected_dispatch_id
            or tool.dispatch_attempt != expected_attempt
            or tool.status is not ToolCallStatus.PENDING
        ):
            raise RuntimeError("Supabase returned another tool retry authorization")
        return tool

    async def record_tool_approval_decision(
        self,
        run_id: str,
        tool_call_id: str,
        decision: ToolApprovalDecision,
    ) -> tuple[ToolCallRecord, RunEvent | None]:
        result = _object(
            await self._rpc(
                "record_tool_approval_decision",
                {"run_id": run_id, "tool_call_id": tool_call_id, "decision": decision.value},
                capability=self._run_capability(run_id),
            )
        )
        event = result.get("event")
        return self._tool_call(_object(result.get("tool_call"))), (
            None if event is None else self._event(_object(event))
        )

    async def request_tool_approval(
        self,
        run_id: str,
        tool_call_id: str,
        preview: dict[str, object] | None,
    ) -> tuple[RunSnapshot, RunEvent]:
        if (
            preview is not None
            and len(json.dumps(preview, allow_nan=False).encode("utf-8")) > 128_000
        ):
            raise ValueError("approval preview exceeds the storage limit")
        result = _object(
            await self._rpc(
                "request_tool_approval",
                {"run_id": run_id, "tool_call_id": tool_call_id, "preview": preview},
                capability=self._run_capability(run_id),
            )
        )
        return self._run(_object(result.get("run"))), self._event(_object(result.get("event")))

    async def resume_approved_run(self, run_id: str) -> tuple[RunSnapshot, RunEvent]:
        result = _object(
            await self._rpc(
                "resume_approved_run",
                {"run_id": run_id},
                capability=self._run_capability(run_id),
            )
        )
        return self._run(_object(result.get("run"))), self._event(_object(result.get("event")))

    def _assistant_output(self, row: dict[str, object]) -> AssistantOutput:
        self._owned(row)
        status = _text(row, "status")
        if status not in {"streaming", "complete", "partial"}:
            raise RuntimeError("Supabase returned an invalid assistant output status")
        return AssistantOutput(
            id=_text(row, "id"),
            session_id=_text(row, "chat_id"),
            turn_id=_text(row, "turn_id"),
            run_id=_text(row, "run_id"),
            model_call_id=_text(row, "model_call_id"),
            step_index=_integer(row, "step_index"),
            content=_text(row, "content"),
            status=status,
            next_batch_index=_integer(row, "next_batch_index"),
            stop_reason=_nullable_text(row, "stop_reason"),
            promoted_message_id=_nullable_text(row, "promoted_message_id"),
            created_at=_text(row, "created_at"),
        )

    async def append_assistant_batch(
        self, run_id: str, model_call_id: str, batch_index: int, text: str
    ) -> tuple[AssistantOutput, RunEvent]:
        validate_assistant_batch(batch_index, text)
        result = _object(
            await self._rpc(
                "append_assistant_batch",
                {
                    "run_id": run_id,
                    "model_call_id": model_call_id,
                    "batch_index": batch_index,
                    "text": text,
                },
                capability=self._run_capability(run_id),
            )
        )
        return self._assistant_output(_object(result.get("output"))), self._event(
            _object(result.get("event"))
        )

    async def finish_assistant_output(
        self, run_id: str, model_call_id: str, *, stop_reason: str = "tool_continuation"
    ) -> AssistantOutput | None:
        result = await self._rpc(
            "finish_assistant_output",
            {"run_id": run_id, "model_call_id": model_call_id, "stop_reason": stop_reason},
            capability=self._run_capability(run_id),
        )
        return None if result is None else self._assistant_output(_object(result))

    async def list_assistant_outputs(self, session_id: str) -> list[AssistantOutput]:
        rows = await self._read(
            "run_assistant_outputs",
            select="id,user_id,chat_id,turn_id,run_id,model_call_id,step_index,content,status,next_batch_index,stop_reason,promoted_message_id,created_at",
            filters={
                "user_id": f"eq.{self._user_id}",
                "chat_id": f"eq.{session_id}",
                "order": "created_at.asc,id.asc",
            },
        )
        outputs = [self._assistant_output(row) for row in rows]
        if any(item.session_id != session_id for item in outputs):
            raise RuntimeError("Supabase returned assistant output from another chat")
        return outputs

    async def request_run_cancellation(self, run_id: str) -> tuple[RunSnapshot, RunEvent | None]:
        result = _object(
            await self._rpc(
                "request_run_cancellation",
                {"run_id": run_id},
                capability=self._run_capability(run_id),
            )
        )
        event = result.get("event")
        run = self._run(_object(result.get("run")))
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            self._capabilities.pop(run_id, None)
            self._lease_expires.pop(run_id, None)
        return run, None if event is None else self._event(_object(event))

    async def renew_run_lease(self, run_id: str) -> RunSnapshot:
        capability = self._run_capability(run_id)
        try:
            result = _object(
                await self._rpc(
                    "renew_run_lease",
                    {"run_id": run_id},
                    capability=capability,
                )
            )
        except Exception:
            self._drop_lease(run_id)
            raise
        run = self._run(_object(result.get("run")))
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            self._drop_lease(run_id)
            return run
        if result.get("lease_acquired") is not True:
            self._drop_lease(run_id)
            raise ValueError("run has no local lease")
        self._record_lease(run, capability)
        return run

    async def recover_expired_run(self, run_id: str) -> RunSnapshot:
        result = await self._rpc("recover_expired_run", {"run_id": run_id})
        run = self._run(_object(result))
        if run.status is RunStatus.INTERRUPTED:
            self._drop_lease(run_id)
        return run

    async def list_recovery_candidates(self, installation_id: str) -> list[RunSnapshot]:
        rows = _array(
            await self._rpc(
                "list_recovery_candidates", {"installation_id": installation_id, "limit": 20}
            )
        )
        if len(rows) > 20:
            raise RuntimeError("Supabase returned too many recovery candidates")
        return [self._run(row) for row in rows]

    async def claim_recovery(
        self, run: RunSnapshot, installation_id: str
    ) -> tuple[RunSnapshot, str | None]:
        capability = secrets.token_urlsafe(32)
        result = _object(
            await self._idempotent_rpc(
                "claim_recovery",
                {
                    "run_id": run.id,
                    "installation_id": installation_id,
                    "expected_generation": run.generation,
                },
                capability=capability,
            )
        )
        claimed = self._run(_object(result.get("run")))
        if claimed.id != run.id:
            raise RuntimeError("Supabase returned another recovery candidate")
        reason = _nullable_text(result, "rejection_reason")
        if result.get("lease_acquired") is True:
            if claimed.generation != run.generation + 1 or claimed.status not in {
                RunStatus.RECOVERING,
                RunStatus.CANCELLING,
            }:
                raise RuntimeError("Supabase returned an inconsistent recovery takeover")
            self._record_lease(claimed, capability)
            if not self.owns_run_lease(claimed.id):
                raise ApplicationError(
                    "run_lease_lost", "The recovery ownership expired in transit."
                )
        elif reason is None:
            raise RuntimeError("Supabase did not confirm recovery ownership")
        return claimed, reason

    async def relinquish_run(self, run_id: str) -> RunSnapshot:
        result = await self._rpc(
            "relinquish_run", {"run_id": run_id}, capability=self._run_capability(run_id)
        )
        run = self._run(_object(result))
        self._drop_lease(run_id)
        return run

    async def finish_recovery(self, run_id: str) -> tuple[RunSnapshot, RunEvent]:
        result = _object(
            await self._idempotent_rpc(
                "finish_recovery", {"run_id": run_id}, capability=self._run_capability(run_id)
            )
        )
        run, event = (
            self._run(_object(result.get("run"))),
            self._event(_object(result.get("event"))),
        )
        if run.id != run_id or event.run_id != run_id:
            raise RuntimeError("Supabase returned another recovery completion")
        return run, event

    async def stop_recovery(
        self, run_id: str, resolution: RecoveryResolution
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        result = _object(
            await self._idempotent_rpc(
                "stop_recovery",
                {
                    "run_id": run_id,
                    "reason": resolution.reason,
                    "retry_allowed": resolution.retry_allowed,
                    "usage_unknown": resolution.usage_unknown,
                    "model_call_id": resolution.model_call_id,
                    "tool_call_id": resolution.tool_call_id,
                },
                capability=self._run_capability(run_id),
            )
        )
        run = self._run(_object(result.get("run")))
        events = tuple(self._event(row) for row in _array(result.get("events")))
        if (
            run.id != run_id
            or run.status
            not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.INTERRUPTED,
            }
            or any(event.run_id != run_id for event in events)
        ):
            raise RuntimeError("Supabase returned another recovery stop")
        self._drop_lease(run_id)
        return run, events

    async def read_recovery_snapshot(self, run_id: str) -> RecoverySnapshot:
        result = _object(
            await self._request("POST", "rpc/read_recovery_snapshot", payload={"p_run_id": run_id})
        )
        run = self._run(_object(result.get("run")))
        if run.id != run_id:
            raise RuntimeError("Supabase returned another recovery snapshot")
        calls = tuple(self._model_call(row) for row in _array(result.get("model_calls")))
        tools = tuple(self._tool_call(row) for row in _array(result.get("tool_calls")))
        by_message: dict[str, list[ModelToolCall]] = {}
        for call in sorted(tools, key=lambda item: item.call_index):
            by_message.setdefault(call.assistant_message_id, []).append(
                ModelToolCall(call.provider_call_id, call.name, call.arguments)
            )
        exchanges = tuple(
            self._run_message(row, tuple(by_message.get(_text(row, "id"), ())))
            for row in _array(result.get("run_messages"))
        )
        self._model_call_runs.update({call.id: call.run_id for call in calls})
        return RecoverySnapshot(
            run=run,
            model_calls=calls,
            tool_calls=tools,
            run_messages=exchanges,
            messages=tuple(self._message(row) for row in _array(result.get("messages"))),
            outputs=tuple(self._assistant_output(row) for row in _array(result.get("outputs"))),
            context_checkpoints=tuple(
                self._checkpoint(row) for row in _array(result.get("context_checkpoints"))
            ),
            context_revision=_integer(result, "context_revision"),
        )

    async def request_recovery_cancellation(
        self, run_id: str
    ) -> tuple[RunSnapshot, RunEvent | None]:
        result = _object(
            await self._idempotent_rpc(
                "request_recovery_cancellation", {"run_id": run_id}, capability=None
            )
        )
        run = self._run(_object(result.get("run")))
        event = self._event(_object(result["event"])) if result.get("event") is not None else None
        if run.id != run_id or (event is not None and event.run_id != run_id):
            raise RuntimeError("Supabase returned another cancellation request")
        if run.status not in {
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            raise RuntimeError("Supabase did not save the cancellation request")
        return run, event

    async def ensure_account(self) -> None:
        await self._request("POST", "rpc/ensure_account", payload={})
