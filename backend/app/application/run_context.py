"""Bounded checkpoint orchestration shared by generation steps of a run."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import uuid4

from app.application.budgets import PricingUnavailable, count_total_tokens
from app.application.compaction import build_summary_request, replay_checkpoint, summarize_prefix
from app.application.context import build_model_context
from app.application.context_budget import (
    _cost as reserve_context_cost,
)
from app.application.context_budget import (
    estimate_input_tokens,
    normalize_context_items,
    preflight_context,
)
from app.application.errors import ApplicationError, ProviderError
from app.application.model_catalog import (
    ModelProfile,
    UsageTokens,
    estimate_profile_cost,
    profile_for_model,
)
from app.application.ports import (
    ChatRepository,
    ContextCheckpointRepository,
    NativeCompactingProvider,
    RunRepository,
    SessionRepository,
    StreamingProviderAdapter,
    TokenCountingProvider,
)
from app.application.secret_redaction import SecretRedactor
from app.application.telemetry import NO_TELEMETRY, Attribute, Operation, Telemetry
from app.domain.context import (
    CheckpointSource,
    ContextCheckpointDraft,
    ContextItem,
    ContextPreflight,
    InputTokenCount,
    NativeCompactionResult,
)
from app.domain.models import ModelDescriptor
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolSpec,
    RunEvent,
    RunEventType,
    RunSnapshot,
)

Totals = Callable[[str, list[ModelCallRecord]], tuple[int, float | None]]
Publish = Callable[[RunEvent], Awaitable[None]]


@dataclass(slots=True)
class _ModelObservation:
    operation: Operation
    outcome: str = "completed"

    def finish(self, outcome: str, code: str | None = None) -> None:
        self.outcome = outcome
        self.operation.outcome(outcome, code)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _remaining(run: RunSnapshot) -> float:
    seconds = (
        datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00")) - datetime.now(UTC)
    ).total_seconds()
    if seconds <= 0:
        raise ProviderError("provider_timeout", "The run deadline was reached.")
    return seconds


def billing_tier(value: object) -> str:
    return (
        value
        if isinstance(value, str)
        and value in {"standard", "fast", "batch", "flex", "ultrafast", "default", "priority"}
        else "unknown"
    )


def usage_cost(
    model_id: str, usage: dict[str, int | None], billing: Mapping[str, object]
) -> tuple[int | None, float | None]:
    iterations = billing.get("iterations")
    if isinstance(iterations, list):
        tokens, cost = 0, 0.0
        known_tokens = known_cost = billing.get("usage_complete", True) is True
        for raw in iterations:
            if not isinstance(raw, dict):
                raise ValueError("invalid native usage iteration")
            entry = cast(dict[str, object], raw)
            raw_usage = entry.get("usage")
            raw_billing = entry.get("billing")
            if not isinstance(raw_usage, dict) or not isinstance(raw_billing, dict):
                raise ValueError("invalid native usage iteration")
            part_tokens, part_cost = usage_cost(
                model_id,
                cast(dict[str, int | None], raw_usage),
                cast(dict[str, object], raw_billing),
            )
            known_tokens = known_tokens and part_tokens is not None
            known_cost = known_cost and part_cost is not None
            tokens += part_tokens or 0
            cost += part_cost or 0
        return tokens if known_tokens else None, cost if known_cost else None
    profile = profile_for_model(model_id)
    if profile is None or not profile.metadata_ready:
        raise PricingUnavailable(model_id)
    tier = billing.get("service_tier", "standard")
    one_hour = billing.get("cache_creation_1h_tokens")
    pricing = billing.get("pricing_context_tokens")
    created = usage["cache_creation_tokens"]
    if created is None and all(
        item.rates.cache_creation_per_million is None for item in profile.price_tiers
    ):
        created = 0
    # Unknown actual TTL remains unknown; the legacy budget helper deliberately
    # defaults missing TTLs to a conservative one-hour estimate instead.
    actual = UsageTokens(
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cached_tokens"] or 0,
        created,
        one_hour if isinstance(one_hour, int) else None if created else 0,
        usage["reasoning_tokens"],
    )
    known_tier = isinstance(tier, str) and not (
        profile.provider_id != "openai" and tier in {"default", "priority"}
    )
    return (
        count_total_tokens(
            model_id,
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cached_tokens"],
            usage["cache_creation_tokens"],
        ),
        estimate_profile_cost(
            profile,
            actual,
            service_tier=tier if isinstance(tier, str) else "standard",
            pricing_context_tokens=pricing if isinstance(pricing, int) else None,
        )
        if known_tier
        else None,
    )


class RunContext:
    def __init__(
        self,
        runs: RunRepository,
        sessions: SessionRepository,
        checkpoints: ContextCheckpointRepository,
        totals: Totals,
        publish: Publish,
        redactor: SecretRedactor,
        *,
        telemetry: Telemetry = NO_TELEMETRY,
    ) -> None:
        self.telemetry = telemetry
        self.runs = runs
        self.sessions = sessions
        self.checkpoints = checkpoints
        self.totals = totals
        self.publish = publish
        self.redactor = redactor

    async def prepare(
        self,
        run: RunSnapshot,
        model: ModelDescriptor,
        profile: ModelProfile,
        provider: StreamingProviderAdapter,
        api_key: str,
        user_id: str,
        *,
        workspace_root: Path | None,
        guidance: str | None,
        tools: tuple[ModelToolSpec, ...],
    ) -> ContextPreflight:
        with self.telemetry.operation("context.build", {"runId": run.id}) as observation:
            try:
                plan = await self._prepare(
                    run,
                    model,
                    profile,
                    provider,
                    api_key,
                    user_id,
                    workspace_root=workspace_root,
                    guidance=guidance,
                    tools=tools,
                )
            except (ApplicationError, ProviderError) as error:
                observation.outcome("failed", error.code)
                raise
            observation.set_attributes(
                {"context_tokens": plan.input_tokens, "count_source": plan.count_method}
            )
            observation.outcome("completed")
            return plan

    async def _prepare(
        self,
        run: RunSnapshot,
        model: ModelDescriptor,
        profile: ModelProfile,
        provider: StreamingProviderAdapter,
        api_key: str,
        user_id: str,
        *,
        workspace_root: Path | None,
        guidance: str | None,
        tools: tuple[ModelToolSpec, ...],
    ) -> ContextPreflight:
        # Every successful iteration advances an immutable checkpoint. The model
        # call quota bounds both refill and repeated compaction attempts.
        for _ in range(run.max_model_calls):
            calls = await self.runs.list_model_calls(run.id)
            tokens, cost = self.totals(run.model_id, calls)
            if len(calls) >= run.max_model_calls:
                raise ProviderError("model_call_limit", "The run reached its model-call limit.")
            window = await self.checkpoints.load_context_window(run.session_id)
            checkpoint = window.checkpoint
            visible = list(window.messages)
            current_present = any(message.id == run.input_message_id for message in visible)
            if not current_present:
                current = next(
                    (
                        message
                        for message in await cast(ChatRepository, self.sessions).get_turn_messages(
                            run.session_id, run.turn_id
                        )
                        if message.id == run.input_message_id
                    ),
                    None,
                )
                if current is None:
                    raise ApplicationError(
                        "context_current_request_missing",
                        "The current request could not be loaded.",
                    )
                visible.append(current)
            exchange = await self.runs.list_run_messages(run.id)
            if checkpoint is not None and checkpoint.run_id == run.id:
                exchange = [
                    message
                    for message in exchange
                    if message.ordinal > checkpoint.covered_run_message_ordinal
                ]
            tool_calls = await self.runs.list_tool_calls(run.id)
            context = build_model_context(
                visible,
                exchange,
                workspace_root=workspace_root,
                workspace_guidance=guidance,
                run_tool_calls=tool_calls,
                tools=tools,
                redactor=self.redactor,
            )
            request = ModelRequest(
                run.provider_id,
                run.model_id,
                run.adapter_kind,
                run.upstream_model_id,
                context.messages,
                4096,
                context.system_instructions,
                context.tools,
            )
            items = tuple(
                ContextItem(source.id, source.turn_id, message, source.id == run.input_message_id)
                for source, message in zip(
                    sorted(visible, key=lambda value: (value.ordinal, value.id)),
                    context.messages[: len(visible)],
                    strict=True,
                )
            ) + tuple(
                ContextItem(source.id, run.turn_id, message, source_kind="run_message")
                for source, message in zip(
                    sorted(exchange, key=lambda value: (value.ordinal, value.id)),
                    context.messages[len(visible) :],
                    strict=True,
                )
            )
            instruction_hash = _hash(
                {
                    "system": context.system_instructions,
                    "tools": [asdict(tool) for tool in context.tools],
                }
            )
            workspace_hash = _hash(
                {"root": str(workspace_root) if workspace_root else None, "guidance": guidance}
            )
            pricing_context = max(
                checkpoint.draft.pricing_context_tokens if checkpoint else 0,
                self._pricing_highwater(profile, calls),
            )
            if checkpoint is not None:
                native = replay_checkpoint(
                    checkpoint,
                    request,
                    instruction_hash=instruction_hash,
                    workspace_hash=workspace_hash,
                    allow_native=profile.supports_native_compaction is True,
                )
                if native.native_payload_json is not None and current_present:
                    try:
                        native_plan = await self._preflight(
                            run,
                            provider,
                            api_key,
                            user_id,
                            replace(request, native_context_json=native.native_payload_json),
                            items=items,
                            profile=profile,
                            remaining_run_tokens=run.max_total_tokens - tokens,
                            remaining_run_cost_usd=run.max_cost_usd - cost
                            if cost is not None
                            else None,
                            checkpoint_id=checkpoint.id,
                            pricing_context_tokens=pricing_context,
                        )
                        if not native_plan.needs_compaction:
                            return native_plan
                    except ApplicationError as error:
                        if error.code not in {
                            "native_context_count_required",
                            "context_count_mismatch",
                            "context_input_too_large",
                            "run_budget_exhausted",
                            "budget_cost_limit",
                        }:
                            raise
                replay = replay_checkpoint(
                    checkpoint,
                    request,
                    instruction_hash=instruction_hash,
                    workspace_hash=workspace_hash,
                )
                assert replay.message is not None
                marker = ContextItem(
                    checkpoint.id,
                    "checkpoint:" + checkpoint.id,
                    replay.message,
                    source_kind="checkpoint",
                )
                items = (marker, *items)
                request = replace(request, messages=(replay.message, *request.messages))
            plan = await self._preflight(
                run,
                provider,
                api_key,
                user_id,
                request,
                items=items,
                profile=profile,
                remaining_run_tokens=run.max_total_tokens - tokens,
                remaining_run_cost_usd=run.max_cost_usd - cost if cost is not None else None,
                checkpoint_id=checkpoint.id if checkpoint else None,
                pricing_context_tokens=pricing_context,
            )
            if current_present and not plan.needs_compaction:
                return plan
            if not plan.compactable_prefix:
                raise ApplicationError(
                    "context_compaction_unavailable", "No complete history prefix can be compacted."
                )
            if len(calls) + 1 >= run.max_model_calls:
                raise ProviderError(
                    "model_call_limit", "Compaction and generation exceed the model-call limit."
                )
            prefix = plan.compactable_prefix
            while True:
                summary_request = build_summary_request(plan.request, prefix)
                try:
                    summary_plan = await self._preflight(
                        run,
                        provider,
                        api_key,
                        user_id,
                        summary_request,
                        items=(
                            ContextItem(
                                prefix[0].source_id,
                                run.turn_id,
                                summary_request.messages[0],
                                required=True,
                            ),
                        ),
                        profile=profile,
                        remaining_run_tokens=run.max_total_tokens - tokens,
                        remaining_run_cost_usd=run.max_cost_usd - cost
                        if cost is not None
                        else None,
                        pricing_context_tokens=plan.pricing_context_tokens,
                    )
                    break
                except ApplicationError as error:
                    if error.code not in {
                        "run_budget_exhausted",
                        "budget_cost_limit",
                        "context_input_too_large",
                    }:
                        raise
                    last_turn = prefix[-1].turn_id
                    prefix = tuple(item for item in prefix if item.turn_id != last_turn)
                    if not prefix or all(item.source_kind == "checkpoint" for item in prefix):
                        raise ApplicationError(
                            "context_compaction_unavailable",
                            "No affordable complete prefix can be summarized.",
                        ) from None
            native_result = await self._try_native(
                run,
                model,
                profile,
                provider,
                api_key,
                user_id,
                plan,
                summary_plan,
                prefix,
                len(calls),
                tokens,
                cost,
            )
            calls = await self.runs.list_model_calls(run.id)
            tokens, cost = self.totals(run.model_id, calls)
            # Native usage is actual spending. Recheck the private summary
            # against what remains before dispatching another paid request.
            summary_plan = await self._preflight(
                run,
                provider,
                api_key,
                user_id,
                summary_plan.request,
                items=summary_plan.sources,
                profile=profile,
                remaining_run_tokens=run.max_total_tokens - tokens,
                remaining_run_cost_usd=run.max_cost_usd - cost if cost is not None else None,
                pricing_context_tokens=max(
                    plan.pricing_context_tokens, summary_plan.pricing_context_tokens
                ),
            )
            summary, call = await self._summarize(
                run, model, provider, summary_plan, api_key, user_id, len(calls) + 1
            )
            after_calls = await self.runs.list_model_calls(run.id)
            spent_tokens, spent_cost = self.totals(run.model_id, after_calls)
            if (
                spent_tokens >= run.max_total_tokens
                or spent_cost is None
                or spent_cost >= run.max_cost_usd
            ):
                raise ApplicationError(
                    "run_budget_exhausted", "Compaction exhausted the remaining run budget."
                )
            after = replace(
                request,
                messages=(
                    ModelMessage(
                        "user", "Saved conversation summary (historical context):\n" + summary
                    ),
                    *(item.message for item in plan.sources[len(prefix) :]),
                ),
            )
            draft = ContextCheckpointDraft(
                str(uuid4()),
                call.id,
                tuple(
                    CheckpointSource(item.source_id, item.source_kind, item.turn_id)
                    for item in prefix
                ),
                summary,
                _hash([asdict(item) for item in prefix]),
                plan.input_hash,
                instruction_hash,
                workspace_hash,
                plan.input_tokens,
                estimate_input_tokens(after).tokens,
                native_kind=native_result.native_kind if native_result else None,
                native_payload_json=native_result.payload_json if native_result else None,
                pricing_context_tokens=max(
                    plan.pricing_context_tokens,
                    summary_plan.pricing_context_tokens,
                    self._pricing_highwater(profile, after_calls),
                ),
            )
            await self.checkpoints.save_context_checkpoint(
                run.id, checkpoint.revision if checkpoint else 0, draft
            )
        raise ProviderError("model_call_limit", "The run reached its model-call limit.")

    @staticmethod
    def _pricing_highwater(profile: ModelProfile, calls: list[ModelCallRecord]) -> int:
        highwater = 0
        for call in calls:
            billing = (call.response_snapshot or {}).get("usage_context", {})
            if not isinstance(billing, dict):
                continue
            prior = billing.get("pricing_context_tokens")
            if isinstance(prior, int):
                highwater = max(highwater, prior)
            iterations = billing.get("iterations")
            if isinstance(iterations, list):
                # Iterations are separate requests. Their summed input is billed
                # usage, not the size of one pricing context.
                for entry in iterations:
                    if not isinstance(entry, dict) or not isinstance(entry.get("usage"), dict):
                        continue
                    part = entry["usage"]
                    prompt = part.get("input_tokens")
                    if not isinstance(prompt, int):
                        continue
                    if not profile.cache_tokens_included_in_input:
                        prompt += (part.get("cached_tokens") or 0) + (
                            part.get("cache_creation_tokens") or 0
                        )
                    highwater = max(highwater, prompt)
            elif call.input_tokens is not None:
                prompt = call.input_tokens
                if not profile.cache_tokens_included_in_input:
                    prompt += (call.cached_tokens or 0) + (call.cache_creation_tokens or 0)
                highwater = max(highwater, prompt)
        return highwater

    async def _try_native(
        self,
        run: RunSnapshot,
        model: ModelDescriptor,
        profile: ModelProfile,
        provider: StreamingProviderAdapter,
        api_key: str,
        user_id: str,
        plan: ContextPreflight,
        summary_plan: ContextPreflight,
        prefix: tuple[ContextItem, ...],
        call_count: int,
        tokens: int,
        cost: float | None,
    ) -> NativeCompactionResult | None:
        if (
            profile.supports_native_compaction is not True
            or not callable(getattr(provider, "compact", None))
            or not callable(getattr(provider, "count_input", None))
            or call_count + 2 >= run.max_model_calls
            or cost is None
        ):
            return None
        native_request = replace(
            plan.request,
            messages=tuple(item.message for item in prefix),
            native_context_json=None,
            max_output_tokens=2048,
            tools=() if profile.provider_id == "openai" else plan.request.tools,
        )
        try:
            native_plan = await self._preflight(
                run,
                provider,
                api_key,
                user_id,
                native_request,
                items=tuple(replace(item, required=True) for item in prefix),
                profile=profile,
                remaining_run_tokens=run.max_total_tokens - tokens,
                remaining_run_cost_usd=run.max_cost_usd - cost,
                pricing_context_tokens=plan.pricing_context_tokens,
            )
        except ApplicationError as error:
            if error.code in {
                "context_input_too_large",
                "run_budget_exhausted",
                "budget_cost_limit",
            }:
                return None
            raise
        if native_plan.count_method != "provider":
            return None
        # OpenAI compact has no output cap. Reserve the verified model maximum
        # before dispatch, plus both the portable companion and next generation.
        output = profile.max_output_tokens if profile.provider_id == "openai" else 2048
        assert output is not None
        window = profile.context_window_tokens
        assert window is not None
        native_input_cap = min(window - output, profile.max_input_tokens or window)
        native_input_cap -= max(2048, (window + 49) // 50)
        if native_plan.input_tokens > native_input_cap:
            return None
        native_cost = reserve_context_cost(
            profile, native_plan.input_tokens, output, plan.pricing_context_tokens
        )
        summary_tokens = summary_plan.input_tokens + summary_plan.output_reserve_tokens
        generation_tokens = plan.input_tokens + plan.output_reserve_tokens
        if (
            native_cost is None
            or native_plan.input_tokens + output + summary_tokens + generation_tokens
            > run.max_total_tokens - tokens
            or native_cost
            + (summary_plan.estimated_call_cost_usd or 0)
            + (plan.estimated_call_cost_usd or 0)
            > run.max_cost_usd - cost
        ):
            return None
        native_plan = replace(
            native_plan,
            request=replace(native_plan.request, max_output_tokens=output),
            output_reserve_tokens=output,
            estimated_call_cost_usd=native_cost,
        )
        return await self._compact(
            run, model, provider, native_plan, api_key, user_id, call_count + 1
        )

    @contextmanager
    def _model_operation(
        self, run: RunSnapshot, call: ModelCallRecord, purpose: str
    ) -> Iterator[_ModelObservation]:
        attributes: dict[str, Attribute] = {
            "runId": run.id,
            "modelCallId": call.id,
            "provider": run.provider_id,
            "model": run.model_id,
            "purpose": purpose,
        }
        started = monotonic()
        with self.telemetry.operation("model.call", attributes, kind="client") as operation:
            observation = _ModelObservation(operation)
            try:
                yield observation
            except asyncio.CancelledError:
                observation.finish("cancelled")
                raise
            except (ApplicationError, ProviderError) as error:
                observation.finish("failed", error.code)
                raise
            except BaseException:
                observation.finish("failed", "model_internal_error")
                raise
            finally:
                attributes["outcome"] = observation.outcome
                self.telemetry.event(
                    "model.call.finished", attributes, error=observation.outcome == "failed"
                )
                self.telemetry.count("model.calls", attributes=attributes)
                self.telemetry.observe("model.call.duration", monotonic() - started, attributes)

    def _observe_usage(
        self,
        observation: _ModelObservation,
        total: int | None,
        cost: float | None,
        *,
        metrics: bool = True,
    ) -> None:
        attributes: dict[str, Attribute] = {}
        if total is not None:
            attributes["total_tokens"] = total
            if metrics:
                self.telemetry.observe("model.tokens", total)
        if cost is not None:
            attributes["estimated_cost_usd"] = cost
            if metrics:
                self.telemetry.observe("model.estimated_cost", cost)
        observation.operation.set_attributes(attributes)

    async def _compact(
        self,
        run: RunSnapshot,
        model: ModelDescriptor,
        provider: StreamingProviderAdapter,
        plan: ContextPreflight,
        api_key: str,
        user_id: str,
        step_index: int,
    ) -> NativeCompactionResult | None:
        call = await self.runs.create_model_call(
            run.id,
            step_index,
            model,
            {
                "purpose": "native_compaction",
                "context": plan.snapshot(),
                "messages": [asdict(message) for message in plan.request.messages],
                "system_instructions": plan.request.system_instructions,
                "tools": [asdict(tool) for tool in plan.request.tools],
                "max_output_tokens": plan.request.max_output_tokens,
            },
        )
        with self._model_operation(run, call, "native_compaction") as observation:
            usage: dict[str, int | None] = {
                key: None
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "cache_creation_tokens",
                    "reasoning_tokens",
                )
            }
            iterations: list[dict[str, object]] = []
            billing: dict[str, object] = {
                "pricing_context_tokens": plan.pricing_context_tokens,
                "iterations": iterations,
                "usage_complete": False,
            }
            try:
                call = await self.runs.update_model_call(call.id, ModelCallStatus.STREAMING)
                await self.publish(
                    await self.runs.append_run_event(
                        run.id,
                        RunEventType.PHASE,
                        {"phase": "compacting", "model_call_id": call.id},
                    )
                )
                try:
                    async with asyncio.timeout(_remaining(run)):
                        result = await cast(NativeCompactingProvider, provider).compact(
                            plan.request, api_key, user_id
                        )
                except TimeoutError:
                    raise ProviderError(
                        "provider_timeout", "The run deadline was reached."
                    ) from None
                for event in result.usage_events:
                    if event.kind != "usage":
                        raise ProviderError(
                            "native_usage_unknown", "Native compaction usage is unavailable."
                        )
                    part = {key: getattr(event, key) for key in usage}
                    part_billing: dict[str, object] = {
                        "pricing_context_tokens": plan.pricing_context_tokens,
                        "cache_creation_1h_tokens": event.cache_creation_1h_tokens
                        if event.cache_creation_tokens
                        else 0,
                    }
                    if event.service_tier is not None:
                        part_billing["service_tier"] = billing_tier(event.service_tier)
                    iterations.append({"usage": part, "billing": part_billing})
                    for key, value in part.items():
                        if value is not None:
                            usage[key] = (usage[key] or 0) + value
                billing["usage_complete"] = (
                    bool(iterations) and result.failure_code != "native_usage_unknown"
                )
                total, cost = usage_cost(run.model_id, usage, billing)
                self._observe_usage(observation, total, cost)
                call = await self.runs.update_model_call(
                    call.id,
                    ModelCallStatus.STREAMING,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    cache_creation_tokens=usage["cache_creation_tokens"],
                    reasoning_tokens=usage["reasoning_tokens"],
                    estimated_cost=cost,
                    clear_estimated_cost=cost is None,
                    response_snapshot={"usage_context": billing},
                    provider_response_id=self.redactor.redact_text(result.provider_response_id)
                    if result.provider_response_id
                    else None,
                )
                await self.publish(
                    await self.runs.append_run_event(
                        run.id,
                        RunEventType.MODEL_USAGE,
                        {
                            **{key: value for key, value in usage.items() if value is not None},
                            "model_call_id": call.id,
                            "estimated_cost_usd": cost,
                            "purpose": "native_compaction",
                        },
                    )
                )
                if total is None or cost is None:
                    raise ProviderError(
                        "native_usage_unknown", "Native compaction usage is unavailable."
                    )
                valid = result.failure_code is None and result.payload_json is not None
                if valid:
                    assert result.payload_json is not None
                    valid = (
                        len(result.payload_json.encode()) <= 262144
                        and self.redactor.redact_text(result.payload_json) == result.payload_json
                        and result.native_kind
                        == ("openai_window" if run.provider_id == "openai" else "anthropic_block")
                    )
                    try:
                        payload = json.loads(result.payload_json)
                        valid = valid and (
                            isinstance(payload, list) and bool(payload)
                            if run.provider_id == "openai"
                            else isinstance(payload, dict) and payload.get("type") == "compaction"
                        )
                    except ValueError:
                        valid = False
                call = await self.runs.update_model_call(
                    call.id,
                    ModelCallStatus.COMPLETED if valid else ModelCallStatus.FAILED,
                    response_snapshot={
                        "usage_context": billing,
                        "native_kind": result.native_kind,
                        "native_payload_json": result.payload_json if valid else None,
                    },
                    error_code=None if valid else "native_context_invalid",
                )
                observation.finish(
                    "completed" if valid else "failed", None if valid else "native_context_invalid"
                )
                if valid:
                    await self.publish(
                        await self.runs.append_run_event(
                            run.id,
                            RunEventType.MODEL_COMPLETED,
                            {"model_call_id": call.id, "purpose": "native_compaction"},
                        )
                    )
                    return result
                return None
            except BaseException as error:
                if call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
                    await self.runs.update_model_call(
                        call.id,
                        ModelCallStatus.CANCELLED
                        if isinstance(error, asyncio.CancelledError)
                        else ModelCallStatus.FAILED,
                        response_snapshot={"usage_context": billing},
                        clear_estimated_cost=billing["usage_complete"] is not True,
                    )
                raise

    async def _preflight(
        self,
        run: RunSnapshot,
        provider: StreamingProviderAdapter,
        api_key: str,
        user_id: str,
        request: ModelRequest,
        *,
        items: tuple[ContextItem, ...],
        profile: ModelProfile,
        remaining_run_tokens: int,
        remaining_run_cost_usd: float | None,
        checkpoint_id: str | None = None,
        pricing_context_tokens: int = 0,
    ) -> ContextPreflight:
        normalized = normalize_context_items(items)
        request = replace(request, messages=tuple(item.message for item in normalized))
        counted: InputTokenCount | None = None
        if callable(getattr(provider, "count_input", None)):
            with self.telemetry.operation(
                "model.count_input",
                {"runId": run.id, "provider": run.provider_id, "model": run.model_id},
                kind="client",
            ) as observation:
                try:
                    async with asyncio.timeout(min(30.0, _remaining(run))):
                        counted = await cast(TokenCountingProvider, provider).count_input(
                            request, api_key, user_id
                        )
                    observation.outcome("completed")
                except ProviderError, TimeoutError:
                    # Native state has no conservative byte-to-token fallback.
                    observation.outcome("fallback", "token_count_unavailable")
                    counted = None
        return preflight_context(
            request,
            items=normalized,
            profile=profile,
            remaining_run_tokens=remaining_run_tokens,
            counted_input=counted,
            checkpoint_id=checkpoint_id,
            remaining_run_cost_usd=remaining_run_cost_usd,
            pricing_context_tokens=pricing_context_tokens,
        )

    async def _summarize(
        self,
        run: RunSnapshot,
        model: ModelDescriptor,
        provider: StreamingProviderAdapter,
        plan: ContextPreflight,
        api_key: str,
        user_id: str,
        step_index: int,
    ) -> tuple[str, ModelCallRecord]:
        request = plan.request
        snapshot: dict[str, object] = {
            "purpose": "summary",
            "context": plan.snapshot(),
            "messages": [asdict(message) for message in request.messages],
            "system_instructions": request.system_instructions,
            "max_output_tokens": request.max_output_tokens,
            "tools": [],
        }
        call = await self.runs.create_model_call(run.id, step_index, model, snapshot)
        with self._model_operation(run, call, "summary") as observation:
            usage: dict[str, int | None] = {
                key: None
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "cache_creation_tokens",
                    "reasoning_tokens",
                )
            }
            billing: dict[str, object] = {"pricing_context_tokens": plan.pricing_context_tokens}

            async def record(event: ModelStreamEvent) -> None:
                nonlocal call
                usage.update(
                    {key: value for key in usage if (value := getattr(event, key)) is not None}
                )
                if event.service_tier is not None:
                    billing["service_tier"] = billing_tier(event.service_tier)
                if event.cache_creation_tokens is not None:
                    billing["cache_creation_1h_tokens"] = (
                        event.cache_creation_1h_tokens if event.cache_creation_tokens else 0
                    )
                total, cost = usage_cost(run.model_id, usage, billing)
                self._observe_usage(observation, total, cost, metrics=False)
                call = await self.runs.update_model_call(
                    call.id,
                    ModelCallStatus.STREAMING,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    cache_creation_tokens=usage["cache_creation_tokens"],
                    reasoning_tokens=usage["reasoning_tokens"],
                    estimated_cost=cost,
                    clear_estimated_cost=cost is None,
                    response_snapshot={"usage_context": billing.copy()},
                )
                await self.publish(
                    await self.runs.append_run_event(
                        run.id,
                        RunEventType.MODEL_USAGE,
                        {
                            **{key: value for key, value in usage.items() if value is not None},
                            "model_call_id": call.id,
                            "estimated_cost_usd": cost,
                        },
                    )
                )

            try:
                call = await self.runs.update_model_call(call.id, ModelCallStatus.STREAMING)
                await self.publish(
                    await self.runs.append_run_event(
                        run.id,
                        RunEventType.PHASE,
                        {"phase": "compacting", "model_call_id": call.id},
                    )
                )
                result = await summarize_prefix(
                    provider,
                    request,
                    api_key,
                    user_id,
                    timeout_seconds=_remaining(run),
                    redactor=self.redactor,
                    on_usage=record,
                )
                total, cost = usage_cost(run.model_id, usage, billing)
                self._observe_usage(observation, total, cost)
                if total is None or cost is None:
                    raise ProviderError(
                        "provider_usage_unavailable", "The summary did not report enough usage."
                    )
                call = await self.runs.update_model_call(
                    call.id,
                    ModelCallStatus.COMPLETED,
                    provider_response_id=self.redactor.redact_text(result.provider_response_id)
                    if result.provider_response_id
                    else None,
                    response_snapshot={"content": result.text, "usage_context": billing.copy()},
                    estimated_cost=cost,
                    clear_estimated_cost=cost is None,
                )
                await self.publish(
                    await self.runs.append_run_event(
                        run.id,
                        RunEventType.MODEL_COMPLETED,
                        {"model_call_id": call.id, "purpose": "summary"},
                    )
                )
                observation.finish("completed")
                return result.text, call
            except BaseException as error:
                status = (
                    ModelCallStatus.CANCELLED
                    if isinstance(error, asyncio.CancelledError)
                    else ModelCallStatus.FAILED
                )
                if call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
                    await self.runs.update_model_call(call.id, status)
                raise
