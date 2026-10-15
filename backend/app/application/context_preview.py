"""Local, read-only estimates for an unsent chat draft.

The preview uses a portable checkpoint rather than opaque provider state, so it
is an estimate even when the last generation used provider token counting.
"""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from app.application.budgets import BudgetPreset, limits_for_preset
from app.application.context import build_model_context
from app.application.context_budget import estimate_input_tokens
from app.application.errors import ApplicationError
from app.application.model_catalog import context_budget, profile_for_model
from app.application.ports import (
    ContextCheckpointRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    WorkspaceBindingPort,
)
from app.application.secret_redaction import COMMON_SECRET_REDACTOR, SecretRedactor
from app.application.settings import is_selectable
from app.application.tools import ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.context import ContextPreflight
from app.domain.runtime import ModelMessage, ModelRequest

ContextStatus = Literal["ready", "compaction_required", "compacting", "unavailable"]


@dataclass(frozen=True, slots=True)
class ContextPreview:
    model_id: str
    context_window_tokens: int | None
    input_tokens: int | None
    count_method: Literal["provider", "utf8_bytes_estimate_v1"]
    output_reserve_tokens: int | None
    safety_margin_tokens: int | None
    usable_input_tokens: int | None
    compaction_threshold_tokens: int | None
    percentage: float | None
    status: ContextStatus
    checkpoint_id: str | None = None

    def snapshot(self) -> dict[str, object]:
        return asdict(self)


def preview_from_plan(
    plan: ContextPreflight,
    *,
    context_window_tokens: int,
    status: ContextStatus | None = None,
) -> ContextPreview:
    """Share the public numerical contract with durable run.context events."""
    return ContextPreview(
        plan.request.model_id,
        context_window_tokens,
        plan.input_tokens,
        "provider" if plan.count_method == "provider" else "utf8_bytes_estimate_v1",
        plan.output_reserve_tokens,
        plan.safety_margin_tokens,
        plan.usable_input_tokens,
        plan.usable_input_tokens * 4 // 5,
        min(100.0, plan.input_tokens * 100 / context_window_tokens),
        status or ("compaction_required" if plan.needs_compaction else "ready"),
        plan.checkpoint_id,
    )


def _unavailable(model_id: str) -> ContextPreview:
    return ContextPreview(
        model_id, None, None, "utf8_bytes_estimate_v1", None, None, None, None, None, "unavailable"
    )


class ContextPreviewService:
    def __init__(
        self,
        sessions: SessionRepository,
        settings: SettingsRepository,
        checkpoints: ContextCheckpointRepository,
        credentials: SecretStorePort,
        *,
        available_adapters: frozenset[str],
        tool_registry: ToolRegistry | None = None,
        capabilities: RuntimeCapabilities | None = None,
        redactor: SecretRedactor = COMMON_SECRET_REDACTOR,
        workspace_bindings: WorkspaceBindingPort | None = None,
        workspace_guidance_reader: Callable[[Path], Awaitable[str | None]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._checkpoints = checkpoints
        self._credentials = credentials
        self._available_adapters = available_adapters
        self._tool_registry = tool_registry
        self._capabilities = capabilities or RuntimeCapabilities()
        self._redactor = redactor
        self._workspace_bindings = workspace_bindings
        self._workspace_guidance_reader = workspace_guidance_reader

    async def preview(
        self,
        *,
        model_id: str,
        draft: str,
        budget_preset: BudgetPreset,
        session_id: str | None = None,
    ) -> ContextPreview:
        session = await self._sessions.get_session(session_id) if session_id is not None else None
        if session_id is not None and session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        model = next(
            (model for model in await self._settings.list_catalog_models() if model.id == model_id),
            None,
        )
        profile = profile_for_model(model_id)
        if (
            model is None
            or not is_selectable(model)
            or profile is None
            or model.adapter_kind not in self._available_adapters
            or (
                model.requires_api_key
                and not (await self._credentials.status(model.provider_id))[0]
            )
        ):
            return _unavailable(model_id)
        budget = context_budget(profile)
        if budget is None:
            return _unavailable(model_id)
        window = (
            await self._checkpoints.load_context_window(session.id, limit=128)
            if session is not None
            else None
        )
        workspace_root = (
            await self._workspace_bindings.resolve(session.id, session.workspace_path)
            if session is not None
            and self._capabilities.workspace_enabled
            and self._workspace_bindings is not None
            else None
        )
        guidance = (
            await self._workspace_guidance_reader(workspace_root)
            if self._capabilities.allows("local_read")
            and workspace_root is not None
            and self._workspace_guidance_reader is not None
            else None
        )
        tools = (
            self._tool_registry.specs(workspace_root, capabilities=self._capabilities)
            if model.supports_tools and self._tool_registry is not None
            else ()
        )
        context = build_model_context(
            window.messages if window else (),
            (),
            workspace_root=workspace_root,
            workspace_guidance=guidance,
            tools=tools,
            redactor=self._redactor,
        )
        checkpoint = window.checkpoint if window else None
        prefix = (
            (
                ModelMessage(
                    "user",
                    "Saved conversation summary (historical context):\n"
                    + self._redactor.redact_text(checkpoint.draft.summary),
                ),
            )
            if checkpoint is not None
            else ()
        )
        request = ModelRequest(
            model.provider_id,
            model.id,
            model.adapter_kind,
            model.upstream_model_id,
            (
                *prefix,
                *context.messages,
                ModelMessage("user", self._redactor.redact_text(draft.strip())),
            ),
            budget.output_reserve_tokens,
            context.system_instructions,
            context.tools,
        )
        count = estimate_input_tokens(request)
        usable = min(
            budget.usable_input_tokens,
            limits_for_preset(budget_preset).max_total_tokens - budget.output_reserve_tokens,
        )
        threshold = usable * 4 // 5
        return ContextPreview(
            model_id,
            budget.context_window_tokens,
            count.tokens,
            "utf8_bytes_estimate_v1",
            budget.output_reserve_tokens,
            budget.safety_margin_tokens,
            usable,
            threshold,
            min(100.0, count.tokens * 100 / budget.context_window_tokens),
            "compaction_required"
            if count.tokens >= threshold or (window is not None and len(window.messages) == 128)
            else "ready",
            checkpoint.id if checkpoint else None,
        )
