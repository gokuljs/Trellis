import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from app.application.budgets import (
    BudgetExceeded,
    PricingUnavailable,
    RunLimits,
    check_resource_budget,
    check_step_budget,
    count_total_tokens,
    estimate_model_cost,
)
from app.application.context_preview import preview_from_plan
from app.application.errors import ApplicationError, ProviderError
from app.application.model_catalog import profile_for_model
from app.application.ports import (
    ContextCheckpointRepository,
    RunEventPublisher,
    RunLeasePort,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    StreamingProviderAdapter,
    WorkspaceBindingPort,
)
from app.application.run_context import RunContext, billing_tier, usage_cost
from app.application.secret_redaction import COMMON_SECRET_REDACTOR, SecretRedactor
from app.application.streaming import TextStreamBatcher
from app.application.tools import (
    MAX_ARGUMENT_BYTES,
    ToolExecutionError,
    ToolRegistry,
)
from app.application.worker_ports import WorkerExecutionRepository
from app.domain.capabilities import RuntimeCapabilities
from app.domain.jobs import RunExecutionContext
from app.domain.models import ModelDescriptor
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelContinuationItem,
    ModelFinishKind,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    ToolResult,
    is_terminal_run_status,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_CONCURRENT_RUNS = 4
DEFAULT_LEASE_RENEW_INTERVAL_SECONDS = 30.0
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
    finish_kind: ModelFinishKind
    billing_context: dict[str, object]


def _usage_measure(
    model_id: str, usage: dict[str, int | None], billing: Mapping[str, object] | None = None
) -> tuple[int | None, float | None]:
    try:
        if billing is not None:
            return usage_cost(model_id, usage, billing)
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
    redactor: SecretRedactor = COMMON_SECRET_REDACTOR,
) -> tuple[tuple[ModelToolCall, ...], dict[str, ToolResult]]:
    safe_calls: list[ModelToolCall] = []
    errors: dict[str, ToolResult] = {}
    for call in calls:
        if (
            len(call.id) > 200
            or len(call.name) > 100
            or redactor.redact_text(call.id) != call.id
            or redactor.redact_text(call.name) != call.name
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
            safe_arguments = cast(dict[str, object], redactor.redact_record(call.arguments))
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


class RunExecutor:
    def __init__(
        self,
        repository: WorkerExecutionRepository,
        secret_store: SecretStorePort,
        providers: Mapping[str, StreamingProviderAdapter],
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._runs: RunRepository = repository
        self._sessions: SessionRepository = repository
        self._secret_store = secret_store
        self._providers = providers
        self._redactor = redactor or COMMON_SECRET_REDACTOR
        self._capabilities = RuntimeCapabilities(frozenset())
        self._tool_registry: ToolRegistry | None = None
        self._workspace_bindings: WorkspaceBindingPort | None = None
        self._workspace_guidance_reader: Callable[[Path], Awaitable[str | None]] | None = None
        self._event_publisher: RunEventPublisher | None = None
        self._lease_repository: RunLeasePort | None = None
        self._lease_lost: set[str] = set()
        self._lease_monitors: dict[str, asyncio.Task[None]] = {}

    def revoke(self, run_id: str) -> None:
        self._lease_lost.add(run_id)

    def _can_execute(self, run_id: str) -> bool:
        return run_id not in self._lease_lost

    async def execute(self, context: RunExecutionContext) -> None:
        if context.run.status is not RunStatus.RUNNING or context.run.deadline_at is None:
            raise ValueError("Execution must begin before provider interaction")
        if context.run.session_id != context.session.id or (
            context.run.model_id,
            context.run.provider_id,
            context.run.adapter_kind,
            context.run.upstream_model_id,
        ) != (
            context.model.id,
            context.model.provider_id,
            context.model.adapter_kind,
            context.model.upstream_model_id,
        ):
            raise ValueError("Execution context does not match the assigned run")
        await self._execute_context(context)

    async def _execute_context(self, context: RunExecutionContext) -> None:
        call: ModelCallRecord | None = None
        started = True
        run_id = context.run.id
        run = context.run
        session = context.session
        model = context.model
        try:
            provider = self._require_provider(model)
            api_key = await self._secret_store.get(run.provider_id)
            if model.requires_api_key and api_key is None:
                raise ApplicationError(
                    "provider_not_configured",
                    "The selected provider is not configured on this server.",
                )
            workspace_root = None
            if self._capabilities.workspace_enabled and self._workspace_bindings is not None:
                workspace_root = await self._workspace_bindings.resolve(
                    session.id, session.workspace_path
                )
            guidance = (
                await self._workspace_guidance_reader(workspace_root)
                if self._capabilities.allows("local_read")
                and workspace_root is not None
                and self._workspace_guidance_reader is not None
                else None
            )
            offered_tools = (
                self._tool_registry.specs(workspace_root, capabilities=self._capabilities)
                if model.supports_tools and self._tool_registry is not None
                else ()
            )
            context_runtime = RunContext(
                self._runs,
                self._sessions,
                cast(ContextCheckpointRepository, self._runs),
                self._budget_totals,
                self._publish,
                self._redactor,
            )

            prior_model_steps = await self._runs.list_model_calls(run.id)
            if prior_model_steps:
                call = prior_model_steps[-1]
                if not await self._execute_pending_tools(run, workspace_root):
                    return

            while True:
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
                model_profile = profile_for_model(run.model_id)
                if model_profile is None:
                    raise ApplicationError(
                        "model_not_available", "The selected model metadata is unavailable."
                    )
                plan = await context_runtime.prepare(
                    run,
                    model,
                    model_profile,
                    provider,
                    api_key or "",
                    context.owner_id,
                    workspace_root=workspace_root,
                    guidance=guidance,
                    tools=offered_tools,
                )
                prior_model_calls = await self._runs.list_model_calls(run.id)
                prior_tokens, prior_cost = self._budget_totals(run.model_id, prior_model_calls)
                step_index = len(prior_model_calls) + 1
                self._check_budget(
                    run,
                    kind="model",
                    model_calls=len(prior_model_calls),
                    tool_calls=len(prior_tool_calls),
                    tokens=prior_tokens,
                    cost_usd=prior_cost,
                )
                request = plan.request
                assert model_profile.context_window_tokens is not None
                await self._append_and_publish(
                    run.id,
                    RunEventType.CONTEXT,
                    preview_from_plan(
                        plan, context_window_tokens=model_profile.context_window_tokens
                    ).snapshot(),
                )
                call = await self._runs.create_model_call(
                    run.id,
                    step_index,
                    model,
                    {**self._request_snapshot(request), "context": plan.snapshot()},
                )
                call = await self._runs.update_model_call(call.id, ModelCallStatus.STREAMING)
                await self._append_and_publish(
                    run.id, RunEventType.PHASE, {"phase": "answering", "model_call_id": call.id}
                )
                step = await self._stream_model_step(
                    run,
                    call,
                    request,
                    provider,
                    api_key or "",
                    context.owner_id,
                    prior_tokens,
                    prior_cost,
                    pricing_context_tokens=plan.pricing_context_tokens,
                )
                if not step.content.strip() and not step.tool_calls:
                    raise ProviderError(
                        "provider_invalid_response", "The provider returned an empty response."
                    )
                safe_calls, preflight_errors = _prepare_tool_calls(step.tool_calls, self._redactor)
                step = replace(step, tool_calls=safe_calls)
                _step_tokens, step_cost = _usage_measure(
                    run.model_id, step.usage, step.billing_context
                )
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
                    clear_estimated_cost=step_cost is None,
                )
                await self._append_and_publish(
                    run.id,
                    RunEventType.MODEL_COMPLETED,
                    {
                        "model_call_id": call.id,
                        "provider_response_id": step.provider_response_id,
                        "finish_reason": step.finish_reason,
                        "finish_kind": step.finish_kind,
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
                if step.finish_kind == "incomplete":
                    raise ProviderError(
                        "provider_response_incomplete",
                        "The provider stopped before completing the answer. Try again.",
                    )
                if (step.finish_kind == "tool") != bool(step.tool_calls):
                    raise ProviderError(
                        "provider_invalid_response",
                        "The provider returned an inconsistent completion.",
                    )
                if not step.tool_calls:
                    final_content = step.content
                    if not final_content.strip():
                        raise ProviderError(
                            "provider_invalid_response", "The provider returned an empty response."
                        )
                    await self._append_and_publish(
                        run.id, RunEventType.PHASE, {"phase": "finishing", "model_call_id": call.id}
                    )
                    _message, completion_events = await self._runs.complete_run(
                        run.id, final_content
                    )
                    for event in completion_events:
                        await self._publish(event)
                    return

                if self._tool_registry is None:
                    raise ProviderError(
                        "tool_execution_unavailable", "This run cannot execute tool requests yet."
                    )
                for tool_request in step.tool_calls:
                    try:
                        self._tool_registry.check_call(
                            tool_request, workspace_root, capabilities=self._capabilities
                        )
                    except ToolExecutionError as error:
                        if error.code == "unknown_tool":
                            raise ProviderError(
                                "provider_invalid_response",
                                "The provider requested an unoffered tool.",
                            ) from None
                        raise ProviderError(error.code, error.message) from None
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
                    content=step.content,
                    tool_calls=step.tool_calls,
                    continuation_items=step.continuation_items,
                )
                await self._runs.finish_assistant_output(run.id, call.id)
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
            if run_id in self._lease_lost:
                raise
            current = await self._runs.get_run(run_id)
            if current is not None and current.status is RunStatus.CANCELLING:
                await self._mark_cancelled(run_id, call)
            elif started and current is not None and current.status is RunStatus.RUNNING:
                await self._mark_interrupted(run_id, call)
            raise
        except ProviderError as error:
            if started and self._can_execute(run_id):
                await self._fail_or_cancel(run_id, call, error.code, error.message)
        except ApplicationError as error:
            if started and self._can_execute(run_id):
                await self._fail_or_cancel(run_id, call, error.code, error.message)
        except Exception as error:
            logger.error(
                "Unexpected failure while executing run %s (error type: %s)",
                run_id,
                type(error).__name__,
            )
            if started and self._can_execute(run_id):
                await self._fail_or_cancel(
                    run_id,
                    call,
                    "runtime_internal_error",
                    "Trellis could not complete this run.",
                )

    async def _renew_before_local_tool(self, run_id: str) -> None:
        if self._lease_repository is None:
            return
        if not self._can_execute(run_id):
            raise ApplicationError("run_lease_lost", "This run is no longer active here.")
        try:
            renewed = await self._lease_repository.renew_run_lease(run_id)
            if is_terminal_run_status(renewed.status):
                raise ApplicationError("run_lease_lost", "This run is no longer active here.")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost.add(run_id)
            monitor = self._lease_monitors.get(run_id)
            if monitor is not None:
                monitor.cancel()
            raise ApplicationError("run_lease_lost", "This run is no longer active here.") from None

    async def _execute_pending_tools(self, run: RunSnapshot, workspace_root: Path | None) -> bool:
        if self._tool_registry is None:
            raise ProviderError("tool_execution_unavailable", "Tool execution is unavailable.")
        model_calls = await self._runs.list_model_calls(run.id)
        all_tool_calls = await self._runs.list_tool_calls(run.id)
        total_tokens, total_cost = self._budget_totals(run.model_id, model_calls)
        for call_index, stored_call in enumerate(all_tool_calls):
            if stored_call.status is not ToolCallStatus.PENDING:
                continue
            await self._renew_before_local_tool(run.id)
            await self._append_and_publish(
                run.id, RunEventType.PHASE, {"phase": "tool_use", "tool_call_id": stored_call.id}
            )
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
            try:
                self._tool_registry.check_call(
                    tool_request, workspace_root, capabilities=self._capabilities
                )
            except ToolExecutionError as error:
                raise ProviderError(error.code, error.message) from None
            approval_required = self._tool_registry.requires_approval(tool_request, workspace_root)
            if approval_required and stored_call.approval_decision is None:
                try:
                    async with asyncio.timeout(remaining_seconds):
                        preview = await self._tool_registry.approval_preview(
                            tool_request, workspace_root, capabilities=self._capabilities
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
                safe_preview = cast(dict[str, object] | None, self._redactor.redact_record(preview))
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
                await self._renew_before_local_tool(run.id)
                async with asyncio.timeout(remaining_seconds):
                    result = await self._tool_registry.execute(
                        tool_request,
                        workspace_root,
                        approved=(stored_call.approval_decision is ToolApprovalDecision.APPROVED),
                        approval_preview=stored_call.approval_preview,
                        capabilities=self._capabilities,
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
            self._redactor.redact_text(result_content),
            status=status
            or (ToolCallStatus.FAILED if result.is_error else ToolCallStatus.COMPLETED),
        )
        await self._publish(event)

    def _request_snapshot(self, request: ModelRequest) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "provider_id": request.provider_id,
            "model_id": request.model_id,
            "adapter_kind": request.adapter_kind,
            "upstream_model_id": request.upstream_model_id,
            "system_instructions": request.system_instructions,
            "messages": [
                {
                    "role": message.role,
                    "content": self._redactor.redact_text(message.content),
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
            **(
                {"native_context_json": request.native_context_json}
                if request.native_context_json is not None
                else {}
            ),
        }
        return cast(dict[str, object], self._redactor.redact_record(snapshot))

    def _response_snapshot(self, step: _ModelStep) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "content": self._redactor.redact_text(step.content),
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
            "usage_context": step.billing_context,
            "finish_reason": step.finish_reason,
            "finish_kind": step.finish_kind,
            "provider_response_id": step.provider_response_id,
        }
        return cast(dict[str, object], self._redactor.redact_record(snapshot))

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
        *,
        pricing_context_tokens: int = 0,
    ) -> _ModelStep:
        output_parts: list[str] = []
        batcher = TextStreamBatcher(secrets=(*self._redactor.streaming_secrets, api_key))
        batch_index = 0

        async def publish_batches(batches: Iterable[str]) -> None:
            nonlocal batch_index
            for text in batches:
                _output, saved_event = await self._runs.append_assistant_batch(
                    run.id, call.id, batch_index, text
                )
                batch_index += 1
                output_parts.append(text)
                await self._publish(saved_event)

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
        billing_context: dict[str, object] = {"pricing_context_tokens": pricing_context_tokens}
        provider_response_id: str | None = None
        finish_reason: str | None = None
        finish_kind: ModelFinishKind | None = None
        received_completion = False
        timeout_seconds = self._remaining_seconds(run)
        if timeout_seconds <= 0:
            raise ProviderError("provider_timeout", "The run deadline was reached.")

        async def process_event(event: ModelStreamEvent) -> None:
            nonlocal output_bytes, continuation_bytes, provider_response_id, finish_reason
            nonlocal finish_kind, received_completion, call
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
                await publish_batches(
                    batcher.push(event.text, now=asyncio.get_running_loop().time())
                )
            elif event.kind == "tool_call":
                if event.tool_call is None:
                    raise ProviderError(
                        "provider_invalid_response",
                        "The provider returned an invalid tool call.",
                    )
                if len(tool_calls) >= run.max_tool_calls:
                    raise ProviderError("tool_call_limit", "The run reached its tool-call limit.")
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
                    or self._redactor.redact_text(item.payload_json) != item.payload_json
                ):
                    raise ProviderError(
                        "provider_invalid_response",
                        "The provider returned invalid continuation data.",
                    )
                continuation_items.append(item)
            elif event.kind == "usage":
                if event.service_tier is not None:
                    billing_context["service_tier"] = billing_tier(event.service_tier)
                if event.cache_creation_tokens is not None:
                    billing_context["cache_creation_1h_tokens"] = (
                        event.cache_creation_1h_tokens if event.cache_creation_tokens else 0
                    )
                incoming_usage = {
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "reasoning_tokens": event.reasoning_tokens,
                    "cached_tokens": event.cached_tokens,
                    "cache_creation_tokens": event.cache_creation_tokens,
                }
                usage.update(
                    {key: value for key, value in incoming_usage.items() if value is not None}
                )
                provider_response_id = (
                    self._safe_provider_metadata(event.provider_response_id) or provider_response_id
                )
                step_tokens, step_cost = _usage_measure(run.model_id, usage, billing_context)
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
                    clear_estimated_cost=step_cost is None,
                    response_snapshot={"usage_context": billing_context.copy()},
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
                    self._safe_provider_metadata(event.provider_response_id) or provider_response_id
                )
                finish_reason = self._safe_provider_metadata(event.finish_reason)
                finish_kind = event.finish_kind

        try:
            async with (
                asyncio.timeout(timeout_seconds),
                aclosing(provider.stream(request, api_key, user_id)) as stream,
            ):
                pending = asyncio.create_task(anext(stream, None))
                try:
                    while True:
                        now = asyncio.get_running_loop().time()
                        deadline = batcher.next_flush_at
                        delay = None if deadline is None else max(0.0, deadline - now)
                        done, _ = await asyncio.wait({pending}, timeout=delay)
                        if not done:
                            await publish_batches(batcher.flush())
                            continue
                        event = pending.result()
                        if event is None:
                            break
                        # Processing remains ordered; only one provider read is in flight.
                        await process_event(event)
                        if received_completion:
                            break
                        pending = asyncio.create_task(anext(stream, None))
                finally:
                    if not pending.done():
                        pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
                await publish_batches(batcher.finish())
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
            finish_kind=finish_kind or ("tool" if tool_calls else "final"),
            billing_context=billing_context,
        )

    def _safe_provider_metadata(self, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 200:
            raise ProviderError(
                "provider_invalid_response", "The provider returned invalid response metadata."
            )
        return self._redactor.redact_text(value)

    @staticmethod
    def _remaining_seconds(run: RunSnapshot) -> float:
        if run.deadline_at is None:
            raise ProviderError("run_not_started", "The run has not begun execution.")
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
            billing = cast(
                Mapping[str, object], (call.response_snapshot or {}).get("usage_context", {})
            )
            if billing.get("usage_complete") is False:
                raise ProviderError(
                    "provider_usage_unavailable",
                    "The provider did not report enough usage to enforce this run's budget.",
                )
            if (
                call.status is not ModelCallStatus.COMPLETED
                and call.input_tokens is None
                and call.output_tokens is None
            ):
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
                billing,
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
        if run.deadline_at is None:
            raise ProviderError("run_not_started", "The run has not begun execution.")
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
                    RunExecutor._limits(run),
                    tokens=tokens,
                    cost_usd=checked_cost,
                    deadline=deadline,
                )
            else:
                check_step_budget(
                    RunExecutor._limits(run),
                    kind=kind,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                    tokens=tokens,
                    cost_usd=checked_cost,
                    deadline=deadline,
                )
        except BudgetExceeded as error:
            raise ProviderError(error.reason, "The run reached its budget limit.") from None

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
        event = await self._runs.append_run_event(
            run_id, event_type, cast(dict[str, object], self._redactor.redact_record(data))
        )
        await self._publish(event)
        return event

    async def _publish(self, event: RunEvent) -> None:
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher.publish(event)
        except Exception as error:
            logger.error(
                "Could not publish persisted event %s for run %s (error type: %s)",
                event.sequence,
                event.run_id,
                type(error).__name__,
            )

    async def _finish_active_model_call(
        self,
        call: ModelCallRecord | None,
        status: ModelCallStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if call is None or call.status not in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            return
        try:
            await self._runs.update_model_call(
                call.id,
                status,
                error_code=error_code,
                error_message=error_message,
            )
        except ApplicationError as error:
            if error.code != "event_storage_limit":
                raise
            # The atomic terminal transition closes calls using reserved capacity.

    async def _fail(
        self,
        run_id: str,
        call: ModelCallRecord | None,
        error_code: str,
        error_message: str,
    ) -> None:
        error_code = self._redactor.redact_text(error_code)
        error_message = self._redactor.redact_text(error_message)
        await self._finish_active_model_call(
            call,
            ModelCallStatus.TIMED_OUT
            if error_code == "provider_timeout"
            else ModelCallStatus.FAILED,
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
        await self._finish_active_model_call(call, ModelCallStatus.CANCELLED)
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
        await self._finish_active_model_call(call, ModelCallStatus.CANCELLED)
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
            try:
                _message, _updated, event = await self._runs.record_tool_result(
                    run_id, tool.id, content, status=status
                )
            except ApplicationError as error:
                if error.code != "event_storage_limit":
                    raise
                # The terminal transition closes remaining tool records without
                # consuming per-tool events from its finite reserved capacity.
                break
            await self._publish(event)
