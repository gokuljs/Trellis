"""RLS-scoped PostgREST repository for web accounts.

The publishable key identifies the Supabase project, while every request uses the
current verified user access token. Mutations are routed through audited SQL RPCs;
run capabilities never leave the backend except in those RPC requests.
"""

import json
import secrets
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import httpx

from app.application.budgets import BudgetPreset, limits_for_preset
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.core.supabase import project_base_url
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
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
)

_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_READ_PAGE_SIZE = 500


class VerifiedTokenSource:
    """Stores only a recent token verified by Supabase Auth for one account."""

    def __init__(self, user_id: UUID) -> None:
        self._user_id = user_id
        self._token: str | None = None
        self._expires_at: datetime | None = None

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


class SupabaseRepository:
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
        count_exact: bool = False,
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
        if count_exact:
            headers["Prefer"] = "count=exact"
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
        if response.status_code in {401, 403}:
            raise ApplicationError("cloud_access_denied", "Your cloud session has expired.")
        error: dict[str, object] = {}
        if response.status_code >= 400:
            with suppress(ValueError, RuntimeError):
                error = _object(response.json())
        error_code = error.get("code")
        error_message = error.get("message")
        if response.status_code == 409:
            if error_code == "PT409" and error_message == "session_workspace_busy":
                raise SessionWorkspaceBusy
            if error_code == "PT409" and error_message == "session_run_busy":
                raise ApplicationError(
                    "session_run_busy", "This chat has an active run. Try again after it finishes."
                )
            if error_code == "PT409" and error_message == "session_turn_busy":
                raise ApplicationError(
                    "session_turn_busy",
                    "This chat has an active turn. Try again after it finishes.",
                )
            if error_code == "PT409" and error_message == "account_import_in_progress":
                raise ApplicationError(
                    "account_import_in_progress",
                    "Account data is being imported. Try again shortly.",
                )
            if error_code == "PT409" and error_message == "account_activity_busy":
                raise ApplicationError(
                    "account_activity_busy",
                    "An active request must finish before account data can be imported.",
                )
            raise ValueError("The cloud record conflicts with the current state")
        if response.status_code == 400 and error_code == "22023":
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
        rows = await self._read(
            "models",
            select=(
                "id,provider_id,provider_name,adapter_kind,upstream_model_id,name,"
                "requires_api_key,supports_streaming,supports_tools,enabled"
            ),
            filters={"enabled": "eq.true", "order": "catalog_order.asc"},
        )
        return [
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
                enabled=row.get("enabled") is True,
            )
            for row in rows
        ]

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

    def _session(self, row: dict[str, object], message_count: int) -> Session:
        self._owned(row)
        return Session(
            id=_text(row, "id"),
            user_id=_text(row, "user_id"),
            title=_text(row, "title"),
            workspace_path=_nullable_text(row, "workspace_path"),
            created_at=_text(row, "created_at"),
            updated_at=_text(row, "updated_at"),
            message_count=message_count,
        )

    async def _count_messages(self, chat_id: str) -> int:
        response = await self._send(
            "HEAD",
            "messages",
            params={
                "select": "id",
                "user_id": f"eq.{self._user_id}",
                "chat_id": f"eq.{chat_id}",
            },
            count_exact=True,
        )
        count = response.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        if not count.isdigit():
            raise RuntimeError("Supabase returned an invalid message count")
        return int(count)

    async def _sessions(self, filters: dict[str, str]) -> list[Session]:
        rows = await self._read(
            "chats",
            select="id,user_id,title,workspace_path,created_at,updated_at",
            filters={"user_id": f"eq.{self._user_id}", **filters},
        )
        return [self._session(row, await self._count_messages(_text(row, "id"))) for row in rows]

    async def list_sessions(self) -> list[Session]:
        return await self._sessions({"order": "updated_at.desc,id.desc"})

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
        rows = await self._sessions({"id": f"eq.{session_id}", "limit": "1"})
        return rows[0] if rows else None

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
                "waiting_tool_call_id"
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

    async def list_runs_for_session(
        self, session_id: str, offset: int, limit: int
    ) -> list[RunSnapshot]:
        if offset < 0 or not 1 <= limit <= 101:
            raise ValueError("run offset and limit are outside the supported range")
        return await self._runs(
            {
                "chat_id": f"eq.{session_id}",
                "order": "created_at.asc,id.asc",
                "offset": str(offset),
                "limit": str(limit),
            }
        )

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
        )

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        rows = await self._read(
            "model_calls",
            select=(
                "id,user_id,run_id,step_index,provider_id,model_id,adapter_kind,status,"
                "request_snapshot,response_snapshot,provider_response_id,finish_reason,"
                "input_tokens,output_tokens,reasoning_tokens,cached_read_tokens,"
                "cache_creation_tokens,estimated_cost,error_code,error_message,started_at,"
                "finished_at"
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
        )

    async def _tool_call_rows(self, run_id: str) -> list[dict[str, object]]:
        return await self._read(
            "tool_calls",
            select=(
                "id,user_id,run_id,assistant_message_id,call_index,provider_call_id,name,"
                "arguments,status,approval_decision,approval_decided_at,approval_preview,"
                "created_at,finished_at"
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
    ) -> RunSnapshot:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("run input cannot be empty")
        if not client_request_id or len(client_request_id) > 200:
            raise ValueError("client request ID must contain 1 to 200 characters")
        if not self._token_source.valid_for(limits_for_preset(budget_preset).max_seconds + 30):
            raise ApplicationError("reauth_required", "Refresh your sign-in before starting a run.")
        capability = secrets.token_urlsafe(32)
        result = _object(
            await self._rpc(
                "create_run",
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "client_request_id": client_request_id,
                    "content": normalized_content,
                    "model_id": model.id,
                    "budget_preset": budget_preset,
                },
                capability=capability,
            )
        )
        run = self._run(_object(result.get("run")))
        if result.get("lease_acquired") is True:
            self._record_lease(run, capability)
        return run

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
                {"run_id": run_id, "content": content.strip()},
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
        result = await self._rpc(
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
        if call.run_id != run_id:
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

    async def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        content: str,
        *,
        status: ToolCallStatus = ToolCallStatus.COMPLETED,
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
            await self._rpc(
                "record_tool_result",
                {
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "status": status.value,
                },
                capability=self._run_capability(run_id),
            )
        )
        return (
            self._run_message(_object(result.get("message"))),
            self._tool_call(_object(result.get("tool_call"))),
            self._event(_object(result.get("event"))),
        )

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

    async def ensure_account(self) -> None:
        await self._request("POST", "rpc/ensure_account", payload={})

    async def import_batch(
        self, table: str, rows: list[dict[str, object]], capability: str
    ) -> None:
        if not rows:
            return
        await self._request(
            "POST",
            "rpc/import_snapshot",
            payload={"p_snapshot": {"table": table, "rows": rows}, "p_capability": capability},
        )

    async def seal_import(self, capability: str) -> None:
        await self._request("POST", "rpc/seal_import", payload={"p_capability": capability})
