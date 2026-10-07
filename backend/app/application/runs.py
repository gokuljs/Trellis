import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from app.application.budgets import (
    BudgetExceeded,
    BudgetPreset,
    PricingUnavailable,
    RunLimits,
    check_resource_budget,
    check_step_budget,
    count_total_tokens,
    estimate_model_cost,
)
from app.application.context import build_model_context
from app.application.errors import ApplicationError, ProviderError
from app.application.ports import (
    ProfileRepository,
    RunEventPublisher,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
)
from app.application.tools import (
    MAX_ARGUMENT_BYTES,
    ToolExecutionError,
    ToolRegistry,
    redact_record,
    redact_secrets,
)
from app.domain.models import ModelDescriptor
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelContinuationItem,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_CONCURRENT_RUNS = 4
MAX_MODEL_RESPONSE_BYTES = 256_000
MAX_CONTINUATION_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class _ModelStep:
    content: str
    tool_calls: tuple[ModelToolCall, ...]
    continuation_items: tuple[ModelContinuationItem, ...]
    usage: dict[str, int | None]
    provider_response_id: str | None
    finish_reason: str | None


def _usage_measure(model_id: str, usage: dict[str, int | None]) -> tuple[int | None, float | None]:
    try:
        tokens = count_total_tokens(
            model_id,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cached_tokens"],
            usage["cache_creation_tokens"],
        )
        cost = estimate_model_cost(
            model_id,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cached_tokens"],
            usage["cache_creation_tokens"],
        )
    except PricingUnavailable:
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if input_tokens is None or output_tokens is None:
            return None, None
        cached_tokens = usage["cached_tokens"] or 0
        cache_creation_tokens = usage["cache_creation_tokens"] or 0
        if min(input_tokens, output_tokens, cached_tokens, cache_creation_tokens) < 0:
            raise ProviderError(
                "provider_invalid_response", "The provider returned invalid usage."
            ) from None
        return input_tokens + output_tokens + cached_tokens + cache_creation_tokens, None
    except ValueError:
        raise ProviderError(
            "provider_invalid_response", "The provider returned invalid usage."
        ) from None
    return tokens, cost


def _prepare_tool_calls(
    calls: tuple[ModelToolCall, ...],
) -> tuple[tuple[ModelToolCall, ...], dict[str, ToolResult]]:
    safe_calls: list[ModelToolCall] = []
    errors: dict[str, ToolResult] = {}
    for call in calls:
        if (
            len(call.id) > 200
            or len(call.name) > 100
            or redact_secrets(call.id) != call.id
            or redact_secrets(call.name) != call.name
        ):
            raise ProviderError(
                "provider_invalid_response", "The provider returned an invalid tool call."
            )
        try:
            encoded = json.dumps(call.arguments, separators=(",", ":"), allow_nan=False)
            oversized = len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES
        except TypeError, ValueError, UnicodeEncodeError:
            oversized = True
        if oversized:
            safe_arguments: dict[str, object] = {}
            errors[call.id] = ToolResult(
                call.id, call.name, "Tool arguments are invalid.", True, "invalid_tool_arguments"
            )
        else:
            safe_arguments = cast(dict[str, object], redact_record(call.arguments))
            if safe_arguments != call.arguments:
                errors[call.id] = ToolResult(
                    call.id,
                    call.name,
                    "Tool arguments contained a secret and were not used.",
                    True,
                    "sensitive_tool_arguments",
                )
        safe_calls.append(ModelToolCall(call.id, call.name, safe_arguments))
    return tuple(safe_calls), errors


class RunService:
    def __init__(
        self,
        runs: RunRepository,
        sessions: SessionRepository,
        settings: SettingsRepository,
        profiles: ProfileRepository,
        secret_store: SecretStorePort,
        providers: Mapping[str, StreamingProviderAdapter],
        event_publisher: RunEventPublisher | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
        workspace_guidance_reader: Callable[[Path], Awaitable[str | None]] | None = None,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("maximum concurrent runs must be positive")
        self._runs = runs
        self._sessions = sessions
        self._settings = settings
        self._profiles = profiles
        self._secret_store = secret_store
        self._providers = providers
        self._event_publisher = event_publisher
        self._tool_registry = tool_registry
        self._workspace_guidance_reader = workspace_guidance_reader
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self._admission_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_finalizers: dict[str, asyncio.Task[None]] = {}
        self._reschedule_pending: set[str] = set()
        self._closing = False

    async def create_run(
        self,
        session_id: str,
        client_request_id: str,
        content: str,
        *,
        turn_id: str | None = None,
        budget_preset: BudgetPreset | None = None,
        model_id: str | None = None,
    ) -> RunSnapshot:
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        normalized_content = content.strip()
        if not normalized_content:
            raise ApplicationError("message_empty", "Message content cannot be empty.")

        models = await self._settings.list_models()
        selected_model_id = (
            model_id if model_id is not None else await self._settings.get_selected_model_id()
        )
        model = next((item for item in models if item.id == selected_model_id), None)
        if model is None:
            raise ApplicationError("model_not_available", "The selected model is unavailable.")
        selected_budget = (
            budget_preset
            if budget_preset is not None
            else await self._settings.get_default_budget_preset()
        )
        if selected_budget not in {"conservative", "longer"}:
            raise ApplicationError("invalid_budget_preset", "Choose an available run budget.")
        self._require_provider(model)
        api_key = await self._secret_store.get(model.provider_id)
        if model.requires_api_key and api_key is None:
            raise ApplicationError(
                "provider_not_configured",
                "Add an API key for the selected provider in Settings.",
            )

        async with self._admission_lock:
            if self._closing:
                raise ApplicationError("runtime_shutting_down", "The runtime is shutting down.")
            try:
                run = await self._runs.create_run(
                    session.id,
                    turn_id or client_request_id,
                    client_request_id,
                    normalized_content,
                    model,
                    budget_preset=selected_budget,
                )
            except ValueError as error:
                message = str(error)
                if "active run already exists" in message:
                    raise ApplicationError(
                        "run_in_progress",
                        "Another run is already in progress for this session.",
                    ) from None
                if "conflicts with a previous payload" in message:
                    raise ApplicationError(
                        "run_conflict",
                        "This request ID is already associated with different run input.",
                    ) from None
                if "session not found" in message:
                    raise ApplicationError("session_not_found", "Session not found.") from None
                raise ApplicationError(
                    "run_invalid", "The run request could not be accepted."
                ) from None

            if run.status is RunStatus.QUEUED:
                self._schedule(run.id)
            return run

    async def wait_for_run(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await task
            finally:
                finalizer = self._cancel_finalizers.get(run_id)
                if finalizer is not None:
                    await finalizer
        else:
            finalizer = self._cancel_finalizers.get(run_id)
            if finalizer is not None:
                await finalizer

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        return await self._runs.get_run(run_id)

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        return await self._runs.get_latest_run_for_session(session_id)

    async def list_session_runs(
        self, session_id: str, offset: int, limit: int
    ) -> tuple[list[RunSnapshot], int | None]:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        runs = await self._runs.list_runs_for_session(session_id, offset, limit + 1)
        return runs[:limit], offset + limit if len(runs) > limit else None

    async def list_session_run_events(
        self, session_id: str, run_id: str, after_sequence: int, limit: int
    ) -> tuple[list[RunEvent], int | None]:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        run = await self._runs.get_run(run_id)
        if run is None or run.session_id != session_id:
            raise ApplicationError("run_not_found", "Run not found.")
        events = await self._runs.list_run_events(run_id, after_sequence, limit=limit)
        next_sequence = (
            events[-1].sequence
            if events and events[-1].sequence < run.last_event_sequence
            else None
        )
        return events, next_sequence

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int,
        *,
        limit: int = 500,
    ) -> list[RunEvent]:
        return await self._runs.list_run_events(run_id, after_sequence, limit=limit)

    async def cancel_run(self, run_id: str) -> RunSnapshot:
        try:
            run, event = await self._runs.request_run_cancellation(run_id)
        except ValueError as error:
            if str(error) == "run not found":
                raise ApplicationError("run_not_found", "Run not found.") from None
            raise
        task = self._tasks.get(run_id)
        if (
            event is not None
            and run.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}
            and task is not None
            and not task.done()
        ):
            task.cancel()
        if event is not None:
            await self._publish(event)
        if event is not None and run.status is RunStatus.CANCELLING:
            current = await self._runs.get_run(run_id)
            if (
                current is not None
                and current.status is RunStatus.CANCELLING
                and task is not None
                and not task.done()
            ):
                self._schedule_cancel_finalizer(run_id, task)
            elif current is not None and current.status is RunStatus.CANCELLING:
                calls = await self._runs.list_model_calls(run_id)
                active_call = next(
                    (
                        call
                        for call in reversed(calls)
                        if call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}
                    ),
                    None,
                )
                await self._mark_cancelled(run_id, active_call)
                persisted = await self._runs.get_run(run_id)
                if persisted is not None:
                    run = persisted
        return run

    def _schedule_cancel_finalizer(self, run_id: str, task: asyncio.Task[None]) -> None:
        existing = self._cancel_finalizers.get(run_id)
        if existing is not None and not existing.done():
            return
        finalizer = asyncio.create_task(
            self._finalize_cancelled_task(run_id, task), name=f"trellis-cancel-{run_id}"
        )
        self._cancel_finalizers[run_id] = finalizer
        finalizer.add_done_callback(lambda done: self._discard_cancel_finalizer(run_id, done))

    async def _finalize_cancelled_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        await asyncio.gather(task, return_exceptions=True)
        current = await self._runs.get_run(run_id)
        if current is not None and current.status is RunStatus.CANCELLING:
            await self._mark_cancelled(run_id, None)

    def _discard_cancel_finalizer(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._cancel_finalizers.get(run_id) is task:
            del self._cancel_finalizers[run_id]

    async def respond_to_tool_approval(
        self,
        run_id: str,
        tool_call_id: str,
        decision: ToolApprovalDecision,
    ) -> tuple[RunSnapshot, ToolCallRecord]:
        if self._closing:
            raise ApplicationError("runtime_shutting_down", "The runtime is shutting down.")
        try:
            tool_call, event = await self._runs.record_tool_approval_decision(
                run_id, tool_call_id, decision
            )
        except ValueError as error:
            message = str(error)
            if message == "run not found":
                raise ApplicationError("run_not_found", "Run not found.") from None
            if message == "tool call not found for run":
                raise ApplicationError("tool_call_not_found", "Tool call not found.") from None
            if message == "tool approval decision already differs":
                raise ApplicationError(
                    "approval_conflict", "This tool approval was already answered differently."
                ) from None
            raise ApplicationError(
                "approval_not_pending", "This tool call is not waiting for approval."
            ) from None
        if event is not None:
            await self._publish(event)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise ApplicationError("run_not_found", "Run not found.")
        if run.status is RunStatus.WAITING_FOR_APPROVAL:
            self._schedule(run.id)
        return run, tool_call

    async def resume_decided_approvals(self) -> None:
        for run_id in await self._runs.list_decided_approval_runs():
            self._schedule(run_id)

    async def close(self) -> None:
        async with self._admission_lock:
            self._closing = True
            pending = tuple(self._tasks.items())
        tasks = tuple(task for _run_id, task in pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        finalizers = tuple(self._cancel_finalizers.values())
        if finalizers:
            await asyncio.gather(*finalizers, return_exceptions=True)
        for run_id, _task in pending:
            run = await self._runs.get_run(run_id)
            if run is not None and run.status is RunStatus.QUEUED:
                await self._interrupt_queued_run(run_id)

    def _schedule(self, run_id: str) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            self._reschedule_pending.add(run_id)
            return
        task = asyncio.create_task(self._run_with_limit(run_id), name=f"trellis-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._discard_task(run_id, done))

    def _discard_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]
        if run_id in self._reschedule_pending:
            self._reschedule_pending.discard(run_id)
            if not self._closing:
                self._schedule(run_id)

    async def _run_with_limit(self, run_id: str) -> None:
        try:
            async with self._slots:
                await self._execute(run_id)
        except asyncio.CancelledError:
            run = await self._runs.get_run(run_id)
            if run is not None and run.status is RunStatus.QUEUED:
                await self._interrupt_queued_run(run_id)
            elif run is not None and run.status is RunStatus.CANCELLING:
                await self._mark_cancelled(run_id, None)
            raise
        except Exception as error:
            logger.error(
                "Unexpected failure before run execution completed for %s (error type: %s)",
                run_id,
                type(error).__name__,
            )
            try:
                run = await self._runs.get_run(run_id)
                if run is not None and run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    event = await self._runs.transition_run_record(
                        run_id,
                        RunStatus.FAILED,
                        RunEventType.FAILED,
                        {
                            "code": "runtime_internal_error",
                            "message": "Trellis could not start this run.",
                        },
                        error_code="runtime_internal_error",
                        error_message="Trellis could not start this run.",
                        stop_reason="runtime_internal_error",
                    )
                    await self._publish(event)
            except Exception as persist_error:
                logger.error(
                    "Could not persist failure for run %s (error type: %s)",
                    run_id,
                    type(persist_error).__name__,
                )

    async def _interrupt_queued_run(self, run_id: str) -> None:
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.INTERRUPTED,
            RunEventType.INTERRUPTED,
            {
                "code": "runtime_shutdown",
                "message": "The run was interrupted during shutdown.",
            },
            error_code="runtime_shutdown",
            error_message="The run was interrupted during shutdown.",
            stop_reason="runtime_shutdown",
        )
        await self._publish(event)

    async def _execute(self, run_id: str) -> None:
        run = await self._runs.get_run(run_id)
        if run is None or run.status not in {
            RunStatus.QUEUED,
            RunStatus.WAITING_FOR_APPROVAL,
        }:
            return

        call: ModelCallRecord | None = None
        started = False
        try:
            if run.status is RunStatus.QUEUED:
                started_event = await self._runs.transition_run_record(
                    run_id,
                    RunStatus.RUNNING,
                    RunEventType.STARTED,
                    {"status": RunStatus.RUNNING.value},
                )
                started = True
                await self._publish(started_event)
            else:
                run, resumed_event = await self._runs.resume_approved_run(run_id)
                started = True
                await self._publish(resumed_event)

            model = await self._get_run_model(run)
            provider = self._require_provider(model)
            api_key = await self._secret_store.get(run.provider_id)
            if model.requires_api_key and api_key is None:
                raise ApplicationError(
                    "provider_not_configured",
                    "Add an API key for the selected provider in Settings.",
                )
            profile = await self._profiles.get_profile()
            session = await self._sessions.get_session(run.session_id)
            if session is None:
                raise ApplicationError("session_not_found", "Session not found.")
            workspace_root = Path(session.workspace_path) if session.workspace_path else None
            guidance = (
                await self._workspace_guidance_reader(workspace_root)
                if workspace_root is not None and self._workspace_guidance_reader is not None
                else None
            )
            offered_tools = (
                self._tool_registry.specs()
                if workspace_root is not None
                and model.supports_tools
                and self._tool_registry is not None
                else ()
            )
            visible_messages = await self._sessions.list_messages(run.session_id)

            prior_model_steps = await self._runs.list_model_calls(run.id)
            if prior_model_steps:
                call = prior_model_steps[-1]
                if not await self._execute_pending_tools(run, workspace_root):
                    return

            for step_index in range(len(prior_model_steps) + 1, run.max_model_calls + 1):
                run_messages = await self._runs.list_run_messages(run.id)
                prior_tool_calls = await self._runs.list_tool_calls(run.id)
                prior_model_calls = await self._runs.list_model_calls(run.id)
                prior_tokens, prior_cost = self._budget_totals(run.model_id, prior_model_calls)
                self._check_budget(
                    run,
                    kind="model",
                    model_calls=len(prior_model_calls),
                    tool_calls=len(prior_tool_calls),
                    tokens=prior_tokens,
                    cost_usd=prior_cost,
                )
                context = build_model_context(
                    visible_messages,
                    run_messages,
                    workspace_root=workspace_root,
                    workspace_guidance=guidance,
                    run_tool_calls=prior_tool_calls,
                    tools=offered_tools,
                )
                request = ModelRequest(
                    provider_id=run.provider_id,
                    model_id=run.model_id,
                    adapter_kind=run.adapter_kind,
                    upstream_model_id=run.upstream_model_id,
                    messages=context.messages,
                    max_output_tokens=min(MAX_OUTPUT_TOKENS, run.max_total_tokens - prior_tokens),
                    system_instructions=context.system_instructions,
                    tools=context.tools,
                )
                call = await self._runs.create_model_call(
                    run.id, step_index, model, self._request_snapshot(request)
                )
                call = await self._runs.update_model_call(call.id, ModelCallStatus.STREAMING)
                step = await self._stream_model_step(
                    run,
                    call,
                    request,
                    provider,
                    api_key or "",
                    profile.id,
                    prior_tokens,
                    prior_cost,
                )
                if not step.content.strip() and not step.tool_calls:
                    raise ProviderError(
                        "provider_invalid_response", "The provider returned an empty response."
                    )
                safe_calls, preflight_errors = _prepare_tool_calls(step.tool_calls)
                step = replace(step, content=redact_secrets(step.content), tool_calls=safe_calls)
                _step_tokens, step_cost = _usage_measure(run.model_id, step.usage)
                call = await self._runs.update_model_call(
                    call.id,
                    ModelCallStatus.COMPLETED,
                    response_snapshot=self._response_snapshot(step),
                    provider_response_id=step.provider_response_id,
                    finish_reason=step.finish_reason,
                    input_tokens=step.usage["input_tokens"],
                    output_tokens=step.usage["output_tokens"],
                    reasoning_tokens=step.usage["reasoning_tokens"],
                    cached_tokens=step.usage["cached_tokens"],
                    cache_creation_tokens=step.usage["cache_creation_tokens"],
                    estimated_cost=step_cost,
                )
                await self._append_and_publish(
                    run.id,
                    RunEventType.MODEL_COMPLETED,
                    {
                        "model_call_id": call.id,
                        "provider_response_id": step.provider_response_id,
                        "finish_reason": step.finish_reason,
                    },
                )
                total_tokens, total_cost = self._budget_totals(
                    run.model_id, [*prior_model_calls, call]
                )
                self._check_budget(
                    run,
                    kind=None,
                    model_calls=step_index,
                    tool_calls=len(prior_tool_calls),
                    tokens=total_tokens,
                    cost_usd=total_cost,
                )
                if not step.tool_calls:
                    final_content = redact_secrets(step.content).strip()
                    if not final_content:
                        raise ProviderError(
                            "provider_invalid_response", "The provider returned an empty response."
                        )
                    await self._append_and_publish(
                        run.id, RunEventType.ASSISTANT_DELTA, {"text": final_content}
                    )
                    _message, completion_events = await self._runs.complete_run(
                        run.id, final_content
                    )
                    for event in completion_events:
                        await self._publish(event)
                    return

                if self._tool_registry is None or workspace_root is None or not request.tools:
                    raise ProviderError(
                        "tool_execution_unavailable", "This run cannot execute tool requests yet."
                    )
                offered_names = {tool.name for tool in request.tools}
                if any(tool.name not in offered_names for tool in step.tool_calls):
                    raise ProviderError(
                        "provider_invalid_response", "The provider requested an unoffered tool."
                    )
                prior_ids = {tool.provider_call_id for tool in prior_tool_calls}
                new_ids = [tool.id for tool in step.tool_calls]
                if len(new_ids) != len(set(new_ids)) or any(
                    tool_id in prior_ids for tool_id in new_ids
                ):
                    raise ProviderError(
                        "provider_invalid_response", "The provider repeated a tool call ID."
                    )
                if len(prior_tool_calls) + len(step.tool_calls) > run.max_tool_calls:
                    raise ProviderError("tool_call_limit", "The run reached its tool-call limit.")
                if step_index >= run.max_model_calls:
                    raise ProviderError("model_call_limit", "The run reached its model-call limit.")
                self._check_budget(
                    run,
                    kind="tool",
                    model_calls=step_index,
                    tool_calls=len(prior_tool_calls),
                    tokens=total_tokens,
                    cost_usd=total_cost,
                )
                assistant_message = ModelMessage(
                    role="assistant",
                    content=redact_secrets(step.content),
                    tool_calls=step.tool_calls,
                    continuation_items=step.continuation_items,
                )
                _assistant, stored_calls, stored_events = await self._runs.record_assistant_message(
                    run.id, call.id, assistant_message
                )
                for event in stored_events:
                    await self._publish(event)
                for stored_call in stored_calls:
                    result = preflight_errors.get(stored_call.provider_call_id)
                    if result is not None:
                        await self._record_tool_result(run.id, stored_call, result)
                if not await self._execute_pending_tools(run, workspace_root):
                    return
            raise ProviderError("model_call_limit", "The run reached its model-call limit.")
        except asyncio.CancelledError:
            current = await self._runs.get_run(run_id)
            if current is not None and current.status is RunStatus.CANCELLING:
                await self._mark_cancelled(run_id, call)
            elif started and current is not None and current.status is RunStatus.RUNNING:
                await self._mark_interrupted(run_id, call)
            raise
        except ProviderError as error:
            if started:
                await self._fail_or_cancel(run_id, call, error.code, error.message)
        except ApplicationError as error:
            if started:
                await self._fail_or_cancel(run_id, call, error.code, error.message)
        except Exception as error:
            logger.error(
                "Unexpected failure while executing run %s (error type: %s)",
                run_id,
                type(error).__name__,
            )
            if started:
                await self._fail_or_cancel(
                    run_id,
                    call,
                    "runtime_internal_error",
                    "Trellis could not complete this run.",
                )

    async def _execute_pending_tools(self, run: RunSnapshot, workspace_root: Path | None) -> bool:
        if self._tool_registry is None or workspace_root is None:
            raise ProviderError("tool_execution_unavailable", "Tool execution is unavailable.")
        model_calls = await self._runs.list_model_calls(run.id)
        all_tool_calls = await self._runs.list_tool_calls(run.id)
        total_tokens, total_cost = self._budget_totals(run.model_id, model_calls)
        for call_index, stored_call in enumerate(all_tool_calls):
            if stored_call.status is not ToolCallStatus.PENDING:
                continue
            self._check_budget(
                run,
                kind="tool",
                model_calls=len(model_calls),
                tool_calls=call_index,
                tokens=total_tokens,
                cost_usd=total_cost,
            )
            remaining_seconds = self._remaining_seconds(run)
            if remaining_seconds <= 0:
                raise ProviderError("provider_timeout", "The run deadline was reached.")
            tool_request = ModelToolCall(
                stored_call.provider_call_id, stored_call.name, stored_call.arguments
            )
            approval_required = self._tool_registry.requires_approval(tool_request, workspace_root)
            if approval_required and stored_call.approval_decision is None:
                try:
                    async with asyncio.timeout(remaining_seconds):
                        preview = await self._tool_registry.approval_preview(
                            tool_request, workspace_root
                        )
                except ToolExecutionError as error:
                    await self._record_tool_result(
                        run.id,
                        stored_call,
                        ToolResult(
                            tool_request.id, tool_request.name, error.message, True, error.code
                        ),
                    )
                    continue
                except TimeoutError:
                    raise ProviderError(
                        "provider_timeout", "The run deadline was reached."
                    ) from None
                safe_preview = cast(dict[str, object] | None, redact_record(preview))
                if safe_preview != preview:
                    await self._record_tool_result(
                        run.id,
                        stored_call,
                        ToolResult(
                            tool_request.id,
                            tool_request.name,
                            "The tool preview contained a secret.",
                            True,
                            "sensitive_approval_preview",
                        ),
                    )
                    continue
                _waiting, event = await self._runs.request_tool_approval(
                    run.id, stored_call.id, safe_preview
                )
                await self._publish(event)
                return False
            if stored_call.approval_decision is ToolApprovalDecision.DENIED:
                result = ToolResult(
                    stored_call.provider_call_id,
                    stored_call.name,
                    "The user denied this tool call.",
                    True,
                    "approval_denied",
                )
                await self._record_tool_result(
                    run.id, stored_call, result, status=ToolCallStatus.DENIED
                )
                continue
            try:
                async with asyncio.timeout(remaining_seconds):
                    result = await self._tool_registry.execute(
                        tool_request,
                        workspace_root,
                        approved=(stored_call.approval_decision is ToolApprovalDecision.APPROVED),
                        approval_preview=stored_call.approval_preview,
                    )
            except TimeoutError:
                raise ProviderError("provider_timeout", "The run deadline was reached.") from None
            await self._record_tool_result(run.id, stored_call, result)
        return True

    async def _record_tool_result(
        self,
        run_id: str,
        stored_call: ToolCallRecord,
        result: ToolResult,
        *,
        status: ToolCallStatus | None = None,
    ) -> None:
        result_content = (
            f"{result.error_code}: {result.content}"
            if result.is_error and result.error_code is not None
            else result.content
        )
        if result.truncated:
            result_content += "\n[Output truncated by Trellis.]"
        _message, _call, event = await self._runs.record_tool_result(
            run_id,
            stored_call.id,
            result_content,
            status=status
            or (ToolCallStatus.FAILED if result.is_error else ToolCallStatus.COMPLETED),
        )
        await self._publish(event)

    @staticmethod
    def _request_snapshot(request: ModelRequest) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "provider_id": request.provider_id,
            "model_id": request.model_id,
            "adapter_kind": request.adapter_kind,
            "upstream_model_id": request.upstream_model_id,
            "system_instructions": request.system_instructions,
            "messages": [
                {
                    "role": message.role,
                    "content": redact_secrets(message.content),
                    **(
                        {"tool_call_id": message.tool_call_id}
                        if message.tool_call_id is not None
                        else {}
                    ),
                    "tool_calls": [
                        {"id": tool.id, "name": tool.name, "arguments": tool.arguments}
                        for tool in message.tool_calls
                    ],
                    "continuation_items": [
                        {
                            "provider_id": item.provider_id,
                            "model_id": item.model_id,
                            "payload_json": item.payload_json,
                        }
                        for item in message.continuation_items
                    ],
                }
                for message in request.messages
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ],
            "max_output_tokens": request.max_output_tokens,
        }
        return cast(dict[str, object], redact_record(snapshot))

    @staticmethod
    def _response_snapshot(step: _ModelStep) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "content": redact_secrets(step.content),
            "tool_calls": [
                {"id": tool.id, "name": tool.name, "arguments": tool.arguments}
                for tool in step.tool_calls
            ],
            "continuation_items": [
                {
                    "provider_id": item.provider_id,
                    "model_id": item.model_id,
                    "payload_json": item.payload_json,
                }
                for item in step.continuation_items
            ],
            "usage": step.usage,
            "finish_reason": step.finish_reason,
            "provider_response_id": step.provider_response_id,
        }
        return cast(dict[str, object], redact_record(snapshot))

    async def _stream_model_step(
        self,
        run: RunSnapshot,
        call: ModelCallRecord,
        request: ModelRequest,
        provider: StreamingProviderAdapter,
        api_key: str,
        user_id: str,
        prior_tokens: int,
        prior_cost: float | None,
    ) -> _ModelStep:
        output_parts: list[str] = []
        output_bytes = 0
        continuation_bytes = 0
        tool_calls: list[ModelToolCall] = []
        continuation_items: list[ModelContinuationItem] = []
        usage: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "cached_tokens": None,
            "cache_creation_tokens": None,
        }
        provider_response_id: str | None = None
        finish_reason: str | None = None
        received_completion = False
        timeout_seconds = self._remaining_seconds(run)
        if timeout_seconds <= 0:
            raise ProviderError("provider_timeout", "The run deadline was reached.")

        try:
            async with asyncio.timeout(timeout_seconds):
                async for event in provider.stream(request, api_key, user_id):
                    if event.kind == "text_delta":
                        if event.text is None:
                            raise ProviderError(
                                "provider_invalid_response", "The provider returned invalid text."
                            )
                        output_bytes += len(event.text.encode("utf-8"))
                        if output_bytes > MAX_MODEL_RESPONSE_BYTES:
                            raise ProviderError(
                                "provider_invalid_response", "The provider response is too large."
                            )
                        output_parts.append(event.text)
                    elif event.kind == "tool_call":
                        if event.tool_call is None:
                            raise ProviderError(
                                "provider_invalid_response",
                                "The provider returned an invalid tool call.",
                            )
                        if len(tool_calls) >= run.max_tool_calls:
                            raise ProviderError(
                                "tool_call_limit", "The run reached its tool-call limit."
                            )
                        tool_calls.append(event.tool_call)
                    elif event.kind == "continuation_item":
                        if event.continuation_item is None:
                            raise ProviderError(
                                "provider_invalid_response",
                                "The provider returned invalid continuation data.",
                            )
                        item = event.continuation_item
                        continuation_bytes += len(item.payload_json.encode("utf-8"))
                        if (
                            continuation_bytes > MAX_CONTINUATION_BYTES
                            or redact_secrets(item.payload_json) != item.payload_json
                        ):
                            raise ProviderError(
                                "provider_invalid_response",
                                "The provider returned invalid continuation data.",
                            )
                        continuation_items.append(item)
                    elif event.kind == "usage":
                        incoming_usage = {
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                            "reasoning_tokens": event.reasoning_tokens,
                            "cached_tokens": event.cached_tokens,
                            "cache_creation_tokens": event.cache_creation_tokens,
                        }
                        usage.update(
                            {
                                key: value
                                for key, value in incoming_usage.items()
                                if value is not None
                            }
                        )
                        provider_response_id = (
                            self._safe_provider_metadata(event.provider_response_id)
                            or provider_response_id
                        )
                        step_tokens, step_cost = _usage_measure(run.model_id, usage)
                        call = await self._runs.update_model_call(
                            call.id,
                            ModelCallStatus.STREAMING,
                            provider_response_id=provider_response_id,
                            input_tokens=usage["input_tokens"],
                            output_tokens=usage["output_tokens"],
                            reasoning_tokens=usage["reasoning_tokens"],
                            cached_tokens=usage["cached_tokens"],
                            cache_creation_tokens=usage["cache_creation_tokens"],
                            estimated_cost=step_cost,
                        )
                        await self._append_and_publish(
                            run.id,
                            RunEventType.MODEL_USAGE,
                            {
                                **{key: value for key, value in usage.items() if value is not None},
                                "estimated_cost_usd": step_cost,
                                "total_estimated_cost_usd": (
                                    prior_cost + step_cost
                                    if step_cost is not None and prior_cost is not None
                                    else None
                                ),
                                "total_tokens": (
                                    prior_tokens + step_tokens if step_tokens is not None else None
                                ),
                                **(
                                    {"provider_response_id": provider_response_id}
                                    if provider_response_id is not None
                                    else {}
                                ),
                            },
                        )
                    elif event.kind == "completed":
                        received_completion = True
                        provider_response_id = (
                            self._safe_provider_metadata(event.provider_response_id)
                            or provider_response_id
                        )
                        finish_reason = self._safe_provider_metadata(event.finish_reason)
        except TimeoutError:
            raise ProviderError("provider_timeout", "The run deadline was reached.") from None

        if not received_completion:
            raise ProviderError(
                "provider_invalid_response", "The provider stream ended before completion."
            )
        return _ModelStep(
            content="".join(output_parts),
            tool_calls=tuple(tool_calls),
            continuation_items=tuple(continuation_items),
            usage=usage,
            provider_response_id=provider_response_id,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _safe_provider_metadata(value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 200:
            raise ProviderError(
                "provider_invalid_response", "The provider returned invalid response metadata."
            )
        return redact_secrets(value)

    @staticmethod
    def _remaining_seconds(run: RunSnapshot) -> float:
        deadline = datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00"))
        return (deadline - datetime.now(UTC)).total_seconds()

    @staticmethod
    def _limits(run: RunSnapshot) -> RunLimits:
        return RunLimits(
            run.max_model_calls,
            run.max_tool_calls,
            run.max_total_tokens,
            0,
            run.max_cost_usd,
        )

    @staticmethod
    def _budget_totals(model_id: str, calls: list[ModelCallRecord]) -> tuple[int, float | None]:
        tokens = 0
        cost = 0.0
        missing_price = False
        for call in calls:
            if call.status is not ModelCallStatus.COMPLETED:
                continue
            call_tokens, call_cost = _usage_measure(
                model_id,
                {
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "reasoning_tokens": call.reasoning_tokens,
                    "cached_tokens": call.cached_tokens,
                    "cache_creation_tokens": call.cache_creation_tokens,
                },
            )
            if call_tokens is None:
                raise ProviderError(
                    "provider_usage_unavailable",
                    "The provider did not report enough usage to enforce this run's budget.",
                )
            tokens += call_tokens
            if call_cost is None:
                missing_price = True
            else:
                cost += call.estimated_cost if call.estimated_cost is not None else call_cost
        return tokens, None if missing_price else cost

    @staticmethod
    def _check_budget(
        run: RunSnapshot,
        *,
        kind: Literal["model", "tool"] | None,
        model_calls: int,
        tool_calls: int,
        tokens: int,
        cost_usd: float | None,
    ) -> None:
        deadline = datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00"))
        if cost_usd is None and kind is not None:
            raise ProviderError(
                "pricing_unavailable",
                "This model has no price estimate for a multi-step run.",
            )
        checked_cost = 0.0 if cost_usd is None else cost_usd
        try:
            if kind is None:
                check_resource_budget(
                    RunService._limits(run),
                    tokens=tokens,
                    cost_usd=checked_cost,
                    deadline=deadline,
                )
            else:
                check_step_budget(
                    RunService._limits(run),
                    kind=kind,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                    tokens=tokens,
                    cost_usd=checked_cost,
                    deadline=deadline,
                )
        except BudgetExceeded as error:
            raise ProviderError(error.reason, "The run reached its budget limit.") from None

    async def _get_run_model(self, run: RunSnapshot) -> ModelDescriptor:
        models = await self._settings.list_models()
        model = next((item for item in models if item.id == run.model_id), None)
        if model is None or (
            model.provider_id,
            model.adapter_kind,
            model.upstream_model_id,
        ) != (run.provider_id, run.adapter_kind, run.upstream_model_id):
            raise ApplicationError("model_not_available", "The run's model is unavailable.")
        return model

    def _require_provider(self, model: ModelDescriptor) -> StreamingProviderAdapter:
        if not model.supports_streaming:
            raise ApplicationError("model_not_streaming", "The selected model cannot stream.")
        provider = self._providers.get(model.adapter_kind)
        if provider is None:
            raise ApplicationError("provider_not_available", "The model adapter is not available.")
        return provider

    async def _append_and_publish(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
    ) -> RunEvent:
        event = await self._runs.append_run_event(run_id, event_type, data)
        await self._publish(event)
        return event

    async def _publish(self, event: RunEvent) -> None:
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher.publish(event)
        except Exception:
            logger.exception(
                "Could not publish persisted event %s for run %s", event.sequence, event.run_id
            )

    async def _fail(
        self,
        run_id: str,
        call: ModelCallRecord | None,
        error_code: str,
        error_message: str,
    ) -> None:
        if call is not None and call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            call_status = (
                ModelCallStatus.TIMED_OUT
                if error_code == "provider_timeout"
                else ModelCallStatus.FAILED
            )
            await self._runs.update_model_call(
                call.id,
                call_status,
                error_code=error_code,
                error_message=error_message,
            )
        await self._finish_pending_tools(
            run_id,
            ToolCallStatus.TIMED_OUT if error_code == "provider_timeout" else ToolCallStatus.FAILED,
            "The tool could not complete.",
        )
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.FAILED,
            RunEventType.FAILED,
            {"code": error_code, "message": error_message},
            error_code=error_code,
            error_message=error_message,
            stop_reason=error_code,
        )
        await self._publish(event)

    async def _fail_or_cancel(
        self,
        run_id: str,
        call: ModelCallRecord | None,
        error_code: str,
        error_message: str,
    ) -> None:
        current = await self._runs.get_run(run_id)
        if current is not None and current.status is RunStatus.CANCELLING:
            await self._mark_cancelled(run_id, call)
        elif current is not None and current.status is RunStatus.RUNNING:
            await self._fail(run_id, call, error_code, error_message)

    async def _mark_interrupted(self, run_id: str, call: ModelCallRecord | None) -> None:
        if call is not None and call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            await self._runs.update_model_call(call.id, ModelCallStatus.CANCELLED)
        await self._finish_pending_tools(
            run_id, ToolCallStatus.CANCELLED, "The run was interrupted."
        )
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.INTERRUPTED,
            RunEventType.INTERRUPTED,
            {"code": "runtime_shutdown", "message": "The run was interrupted during shutdown."},
            error_code="runtime_shutdown",
            error_message="The run was interrupted during shutdown.",
            stop_reason="runtime_shutdown",
        )
        await self._publish(event)

    async def _mark_cancelled(self, run_id: str, call: ModelCallRecord | None) -> None:
        if call is not None and call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            await self._runs.update_model_call(call.id, ModelCallStatus.CANCELLED)
        await self._finish_pending_tools(
            run_id, ToolCallStatus.CANCELLED, "The tool was cancelled."
        )
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.CANCELLED,
            RunEventType.CANCELLED,
            {"code": "user_cancelled", "message": "The run was cancelled."},
            stop_reason="user_cancelled",
        )
        await self._publish(event)

    async def _finish_pending_tools(
        self, run_id: str, status: ToolCallStatus, content: str
    ) -> None:
        for tool in await self._runs.list_tool_calls(run_id):
            if tool.status not in {ToolCallStatus.PENDING, ToolCallStatus.RUNNING}:
                continue
            _message, _updated, event = await self._runs.record_tool_result(
                run_id, tool.id, content, status=status
            )
            await self._publish(event)
