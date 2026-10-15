"""Bounded checkpoint orchestration shared by generation steps of a run."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from app.application.budgets import PricingUnavailable, count_total_tokens
from app.application.compaction import build_summary_request, replay_checkpoint, summarize_prefix
from app.application.context import build_model_context
from app.application.context_budget import (
    estimate_input_tokens,
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
    RunRepository,
    SessionRepository,
    StreamingProviderAdapter,
)
from app.application.secret_redaction import SecretRedactor
from app.domain.context import (
    CheckpointSource,
    ContextCheckpointDraft,
    ContextItem,
    ContextPreflight,
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


def usage_cost(
    model_id: str, usage: dict[str, int | None], billing: Mapping[str, object]
) -> tuple[int | None, float | None]:
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
    ) -> None:
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
            if checkpoint is not None:
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
            pricing_context = checkpoint.draft.pricing_context_tokens if checkpoint else 0
            plan = preflight_context(
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
                    summary_plan = preflight_context(
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
                pricing_context_tokens=max(
                    plan.pricing_context_tokens, summary_plan.pricing_context_tokens
                ),
            )
            await self.checkpoints.save_context_checkpoint(
                run.id, checkpoint.revision if checkpoint else 0, draft
            )
        raise ProviderError("model_call_limit", "The run reached its model-call limit.")

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
        call = await self.runs.update_model_call(call.id, ModelCallStatus.STREAMING)
        await self.publish(
            await self.runs.append_run_event(
                run.id, RunEventType.PHASE, {"phase": "compacting", "model_call_id": call.id}
            )
        )
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
                billing["service_tier"] = event.service_tier
            if event.cache_creation_tokens is not None:
                billing["cache_creation_1h_tokens"] = (
                    event.cache_creation_1h_tokens if event.cache_creation_tokens else 0
                )
            _, cost = usage_cost(run.model_id, usage, billing)
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
