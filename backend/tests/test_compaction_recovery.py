import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from run_fixtures import MODEL, model_record, run_record

from app.application.compaction import build_summary_request
from app.application.context import build_model_context
from app.application.context_budget import input_fingerprint, preflight_context
from app.application.errors import ApplicationError
from app.application.execution_snapshots import (
    plan_snapshot,
    request_snapshot,
    restore_context_plan,
    restore_model_request,
)
from app.application.model_catalog import ModelProfile, profile_for_model
from app.application.ports import ContextCheckpointRepository, RunRepository, SessionRepository
from app.application.run_context import CompactionIntent, RunContext, _hash
from app.application.secret_redaction import SecretRedactor
from app.domain.context import (
    CheckpointSource,
    ContextCheckpoint,
    ContextCheckpointDraft,
    ContextItem,
    ContextWindow,
    InputTokenCount,
    NativeCompactionResult,
)
from app.domain.models import Message
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelContinuationItem,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ModelToolSpec,
    RunEvent,
    RunEventType,
)

PROFILE = cast(ModelProfile, profile_for_model(MODEL.id))


class FileLedger:
    """A durable test ledger: a new instance reads only its committed JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.run = replace(run_record(), input_message_id="f0000000-0000-4000-8000-000000000003")
        self.calls: list[ModelCallRecord] = []
        self.checkpoint: ContextCheckpoint | None = None
        self.events: list[RunEvent] = []
        self.messages = [
            Message(
                "f0000000-0000-4000-8000-000000000001",
                self.run.session_id,
                "old",
                1,
                "user",
                "Inspect the project",
                None,
                None,
                "now",
            ),
            Message(
                "f0000000-0000-4000-8000-000000000002",
                self.run.session_id,
                "old",
                2,
                "assistant",
                "The project uses Python",
                None,
                None,
                "now",
            ),
            Message(
                self.run.input_message_id,
                self.run.session_id,
                self.run.turn_id,
                3,
                "user",
                "Continue",
                None,
                None,
                "now",
            ),
        ]
        if path.exists():
            data = json.loads(path.read_text())
            for row in data["calls"]:
                row["status"] = ModelCallStatus(row["status"])
                self.calls.append(ModelCallRecord(**row))
            self.messages = [Message(**row) for row in data["messages"]]
            if data["checkpoint"]:
                row = data["checkpoint"]
                draft = row.pop("draft")
                draft["sources"] = tuple(CheckpointSource(**item) for item in draft["sources"])
                self.checkpoint = ContextCheckpoint(**row, draft=ContextCheckpointDraft(**draft))
        else:
            self.save()

    def save(self) -> None:
        data = {
            "calls": [asdict(call) for call in self.calls],
            "messages": [asdict(message) for message in self.messages],
            "checkpoint": asdict(self.checkpoint) if self.checkpoint else None,
        }
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(data))
        pending.replace(self.path)

    async def create_model_call(self, run_id, step_index, model, snapshot):
        call = replace(
            model_record(),
            id=str(uuid4()),
            run_id=run_id,
            step_index=step_index,
            request_snapshot=snapshot,
        )
        self.calls.append(call)
        self.save()
        return call

    async def update_model_call(self, call_id, status, **fields):
        call = next(call for call in self.calls if call.id == call_id)
        accepted = {key: value for key, value in fields.items() if key in call.__dataclass_fields__}
        call = replace(call, status=status, **accepted)
        self.calls = [call if previous.id == call.id else previous for previous in self.calls]
        self.save()
        return call

    async def list_model_calls(self, run_id):
        return self.calls.copy()

    async def list_run_messages(self, run_id):
        return []

    async def list_tool_calls(self, run_id):
        return []

    async def append_run_event(self, run_id, event_type, data):
        event = RunEvent(run_id, len(self.events) + 1, event_type, 1, data, "now")
        self.events.append(event)
        return event

    async def load_context_window(self, session_id):
        return ContextWindow(self.checkpoint, tuple(self.messages))

    async def load_context_checkpoint(self, session_id):
        return self.checkpoint

    async def get_turn_messages(self, session_id, turn_id):
        return [message for message in self.messages if message.turn_id == turn_id]

    async def commit_native_compaction(self, run_id, call_id, completion, *, status):
        call = await self.update_model_call(call_id, status, **completion)
        events = ()
        if status is ModelCallStatus.COMPLETED:
            events = (
                await self.append_run_event(
                    run_id,
                    RunEventType.MODEL_COMPLETED,
                    {"model_call_id": call_id, "purpose": "native_compaction"},
                ),
            )
        return call, events

    async def commit_summary(self, run_id, call_id, completion, expected_revision, draft):
        call = next(call for call in self.calls if call.id == call_id)
        call = replace(call, status=ModelCallStatus.COMPLETED, **completion)
        checkpoint = ContextCheckpoint(
            self.run.session_id,
            run_id,
            expected_revision + 1,
            None,
            self.run.provider_id,
            self.run.model_id,
            self.run.adapter_kind,
            2,
            0,
            "now",
            draft,
        )
        self.calls = [call if previous.id == call.id else previous for previous in self.calls]
        self.checkpoint = checkpoint
        self.save()
        event = await self.append_run_event(
            run_id, RunEventType.MODEL_COMPLETED, {"model_call_id": call_id, "purpose": "summary"}
        )
        return call, checkpoint, (event,)


class Provider:
    name: str = "openai"
    model = MODEL.upstream_model_id

    def __init__(self, *, native_valid=True):
        self.native_calls = 0
        self.summary_calls = 0
        self.native_valid = native_valid
        self.requests = []

    async def complete(self, messages, api_key, user_id):
        return "unused"

    async def count_input(self, request, api_key, user_id):
        return InputTokenCount(100, "provider", input_fingerprint(request))

    async def compact(self, request, api_key, user_id):
        self.native_calls += 1
        return NativeCompactionResult(
            "openai_window",
            '[{"type":"compaction","value":"saved"}]' if self.native_valid else None,
            (
                ModelStreamEvent(
                    "usage",
                    input_tokens=100,
                    output_tokens=10,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                ),
            ),
            "native-response",
            None if self.native_valid else "native_context_invalid",
        )

    async def stream(self, request, api_key, user_id):
        self.summary_calls += 1
        self.requests.append(request)
        yield ModelStreamEvent("text_delta", text="The project uses Python.")
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=10, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent(
            "completed", finish_kind="final", provider_response_id="summary-response"
        )


def context(ledger: FileLedger) -> RunContext:
    async def publish(event):
        return None

    def totals(model_id, calls):
        return sum(call.accounted_total_tokens or 0 for call in calls), sum(
            call.estimated_cost or 0 for call in calls
        )

    return RunContext(
        cast(RunRepository, ledger),
        cast(SessionRepository, ledger),
        cast(ContextCheckpointRepository, ledger),
        totals,
        publish,
        SecretRedactor(),
    )


def intent_for(ledger: FileLedger) -> CompactionIntent:
    built = build_model_context(ledger.messages, (), workspace_root=None, tools=())
    request = ModelRequest(
        ledger.run.provider_id,
        ledger.run.model_id,
        ledger.run.adapter_kind,
        ledger.run.upstream_model_id,
        built.messages,
        4096,
        built.system_instructions,
    )
    sources = tuple(
        ContextItem(source.id, source.turn_id, message, source.id == ledger.run.input_message_id)
        for source, message in zip(ledger.messages, request.messages, strict=True)
    )
    plan = preflight_context(
        request,
        items=sources,
        profile=PROFILE,
        remaining_run_tokens=ledger.run.max_total_tokens,
        counted_input=InputTokenCount(100, "provider", input_fingerprint(request)),
        remaining_run_cost_usd=ledger.run.max_cost_usd,
    )
    plan = replace(
        plan, compactable_prefix=sources[:2], retained_tail=sources[2:], needs_compaction=True
    )
    summary = build_summary_request(plan.request, sources[:2])
    summary_sources = (
        ContextItem(sources[0].source_id, ledger.run.turn_id, summary.messages[0], True),
    )
    summary_plan = preflight_context(
        summary,
        items=summary_sources,
        profile=PROFILE,
        remaining_run_tokens=ledger.run.max_total_tokens,
        counted_input=InputTokenCount(100, "provider", input_fingerprint(summary)),
        remaining_run_cost_usd=ledger.run.max_cost_usd,
    )
    return CompactionIntent(
        str(uuid4()),
        str(uuid4()),
        0,
        None,
        plan,
        summary_plan,
        2,
        _hash({"system": built.system_instructions, "tools": []}),
        _hash({"root": None, "guidance": None}),
    )


def native_plan(intent):
    request = replace(
        intent.generation_plan.request,
        messages=tuple(item.message for item in intent.prefix),
        max_output_tokens=2048,
        tools=(),
    )
    sources = tuple(replace(item, required=True) for item in intent.prefix)
    return preflight_context(
        request,
        items=sources,
        profile=PROFILE,
        remaining_run_tokens=100000,
        counted_input=InputTokenCount(100, "provider", input_fingerprint(request)),
        remaining_run_cost_usd=10,
    )


def resume(runtime, ledger, provider, call):
    return runtime.resume_saved_compaction(
        ledger.run,
        MODEL,
        PROFILE,
        provider,
        "key",
        "user",
        call,
        workspace_root=None,
        guidance=None,
        tools=(),
    )


@pytest.mark.parametrize("native_valid", [True, False])
def test_restart_uses_saved_native_result_and_commits_original_summary(tmp_path, native_valid):
    async def exercise():
        path = tmp_path / "ledger.json"
        ledger = FileLedger(path)
        provider = Provider(native_valid=native_valid)
        intent = intent_for(ledger)
        await context(ledger)._compact(
            ledger.run, MODEL, provider, native_plan(intent), "key", "user", 1, intent=intent
        )
        persisted = FileLedger(path)
        checkpoint = await resume(context(persisted), persisted, provider, persisted.calls[0])
        assert (provider.native_calls, provider.summary_calls) == (1, 1)
        assert checkpoint.id == intent.target_checkpoint_id
        assert (
            checkpoint.draft.native_payload_json is not None
            if native_valid
            else checkpoint.draft.native_payload_json is None
        )
        assert checkpoint.draft.model_call_id == persisted.calls[1].id
        assert (
            cast(dict[str, object], persisted.calls[1].request_snapshot["compaction_intent"])[
                "native_model_call_id"
            ]
            == persisted.calls[0].id
        )
        assert [call.accounted_total_tokens for call in persisted.calls] == [110, 110]
        assert all(
            call.status in {ModelCallStatus.COMPLETED, ModelCallStatus.FAILED}
            for call in FileLedger(path).calls
        )
        # An acknowledgment lost after completion never creates another paid call.
        await resume(context(FileLedger(path)), FileLedger(path), provider, persisted.calls[1])
        assert (provider.native_calls, provider.summary_calls) == (1, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("purpose", ["native_compaction", "summary"])
def test_prepared_compaction_dispatches_the_same_saved_request(tmp_path, purpose):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        provider = Provider()
        intent = intent_for(ledger)
        plan = native_plan(intent) if purpose == "native_compaction" else intent.summary_plan
        call = await ledger.create_model_call(
            ledger.run.id,
            1,
            MODEL,
            {
                **request_snapshot(plan.request),
                "purpose": purpose,
                "context": plan.snapshot(),
                "compaction_intent": intent.snapshot(),
            },
        )
        fresh = FileLedger(ledger.path)
        await resume(context(fresh), fresh, provider, fresh.calls[0])
        assert fresh.calls[0].id == call.id
        assert len(fresh.calls) == (2 if purpose == "native_compaction" else 1)
        assert (provider.native_calls, provider.summary_calls) == (
            int(purpose == "native_compaction"),
            1,
        )
        assert provider.requests[0] == intent.summary_plan.request

    asyncio.run(exercise())


@pytest.mark.parametrize("purpose", ["native_compaction", "summary"])
def test_unknown_paid_response_is_never_repeated(tmp_path, purpose):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        plan = native_plan(intent) if purpose == "native_compaction" else intent.summary_plan
        call = await ledger.create_model_call(
            ledger.run.id,
            1,
            MODEL,
            {
                **request_snapshot(plan.request),
                "purpose": purpose,
                "context": plan.snapshot(),
                "compaction_intent": intent.snapshot(),
            },
        )
        call = await ledger.update_model_call(call.id, ModelCallStatus.STREAMING)
        provider = Provider()
        with pytest.raises(ApplicationError, match="dispatched compaction response"):
            await resume(context(ledger), ledger, provider, call)
        assert provider.native_calls == provider.summary_calls == 0
        assert ledger.checkpoint is None

    asyncio.run(exercise())


def test_changed_compaction_sources_stop_before_dispatch(tmp_path):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        call = await ledger.create_model_call(
            ledger.run.id,
            1,
            MODEL,
            {
                **request_snapshot(intent.summary_plan.request),
                "purpose": "summary",
                "context": intent.summary_plan.snapshot(),
                "compaction_intent": intent.snapshot(),
            },
        )
        ledger.messages[0] = replace(ledger.messages[0], content="Changed historical input")
        ledger.save()
        provider = Provider()
        with pytest.raises(ApplicationError, match="inputs have changed"):
            await resume(context(ledger), ledger, provider, call)
        assert provider.native_calls == provider.summary_calls == 0

    asyncio.run(exercise())


def test_execution_snapshots_round_trip_tools_and_opaque_continuation(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.json")
    call = ModelToolCall("provider-call", "read_file", {"path": "README.md"})
    request = replace(
        intent_for(ledger).generation_plan.request,
        messages=(
            ModelMessage(
                "assistant",
                "Inspecting",
                tool_calls=(call,),
                continuation_items=(
                    ModelContinuationItem(
                        ledger.run.provider_id, ledger.run.model_id, '{"opaque":"state"}'
                    ),
                ),
            ),
            ModelMessage("tool", "Saved output", "provider-call"),
        ),
        tools=(ModelToolSpec("read_file", "Read a file", {"type": "object"}),),
    )
    assert (
        restore_model_request(json.loads(json.dumps(request_snapshot(request))), ledger.run)
        == request
    )
    with pytest.raises(ValueError, match="another model"):
        restore_model_request({**request_snapshot(request), "model_id": "other"}, ledger.run)
    intent = intent_for(ledger)
    assert CompactionIntent.restore(json.loads(json.dumps(intent.snapshot())), ledger.run) == intent
    snapshot = plan_snapshot(intent.generation_plan)
    snapshot["input_hash"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        restore_context_plan(snapshot, ledger.run)


@pytest.mark.parametrize("purpose", ["native_compaction", "summary"])
def test_corrupt_prepared_request_is_rejected_before_any_paid_call(tmp_path, purpose):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        plan = native_plan(intent) if purpose == "native_compaction" else intent.summary_plan
        snapshot = {
            **request_snapshot(plan.request),
            "purpose": purpose,
            "context": plan.snapshot(),
            "compaction_intent": intent.snapshot(),
            "system_instructions": "A different instruction",
        }
        call = await ledger.create_model_call(ledger.run.id, 1, MODEL, snapshot)
        provider = Provider()
        with pytest.raises(ApplicationError, match="conflicts with its intent"):
            await resume(context(ledger), ledger, provider, call)
        assert provider.native_calls == provider.summary_calls == 0
        assert ledger.calls[0].status is ModelCallStatus.PENDING

    asyncio.run(exercise())


@pytest.mark.parametrize("limit", ["slots", "tokens", "cost"])
def test_prepared_native_keeps_budget_for_summary_and_generation(tmp_path, limit):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        plan = native_plan(intent)
        call = await ledger.create_model_call(
            ledger.run.id,
            1,
            MODEL,
            {
                **request_snapshot(plan.request),
                "purpose": "native_compaction",
                "context": plan.snapshot(),
                "compaction_intent": intent.snapshot(),
            },
        )
        if limit == "slots":
            ledger.run = replace(ledger.run, max_model_calls=2)
        elif limit == "tokens":
            ledger.run = replace(
                ledger.run, max_total_tokens=plan.input_tokens + plan.output_reserve_tokens + 1
            )
        else:
            ledger.run = replace(
                ledger.run, max_cost_usd=(plan.estimated_call_cost_usd or 0) + 0.000001
            )
        provider = Provider()
        with pytest.raises(ApplicationError):
            await resume(context(ledger), ledger, provider, call)
        assert provider.native_calls == provider.summary_calls == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("corruption", ["intent", "payload", "kind"])
def test_inconsistent_saved_native_stops_before_paying_for_summary(tmp_path, corruption):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        provider = Provider()
        await context(ledger)._compact(
            ledger.run, MODEL, provider, native_plan(intent), "key", "user", 1, intent=intent
        )
        saved = ledger.calls[0]
        if corruption == "intent":
            snapshot = {
                **saved.request_snapshot,
                "compaction_intent": replace(intent, target_checkpoint_id=str(uuid4())).snapshot(),
            }
            ledger.calls[0] = replace(saved, request_snapshot=snapshot)
            # The prepared summary still belongs to the original compaction operation.
            await ledger.create_model_call(
                ledger.run.id,
                2,
                MODEL,
                {
                    **request_snapshot(intent.summary_plan.request),
                    "purpose": "summary",
                    "context": intent.summary_plan.snapshot(),
                    "compaction_intent": replace(intent, native_model_call_id=saved.id).snapshot(),
                },
            )
            selected = ledger.calls[1]
        else:
            response = dict(saved.response_snapshot or {})
            response["native_payload_json" if corruption == "payload" else "native_kind"] = (
                "{}" if corruption == "payload" else "anthropic_block"
            )
            ledger.calls[0] = replace(saved, response_snapshot=response)
            selected = ledger.calls[0]
        with pytest.raises(ApplicationError):
            await resume(context(ledger), ledger, provider, selected)
        assert provider.native_calls == 1 and provider.summary_calls == 0

    asyncio.run(exercise())


def test_summary_checkpoint_pricing_highwater_includes_cached_input(tmp_path):
    class CachedProvider(Provider):
        async def stream(self, request, api_key, user_id):
            yield ModelStreamEvent("text_delta", text="Saved context")
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=1000,
                cache_creation_tokens=500,
                cache_creation_1h_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="final")

    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        model = replace(
            MODEL,
            id="anthropic:claude-sonnet-5",
            provider_id="anthropic",
            adapter_kind="anthropic",
            upstream_model_id="claude-sonnet-5",
        )
        ledger.run = replace(
            ledger.run,
            provider_id=model.provider_id,
            model_id=model.id,
            adapter_kind=model.adapter_kind,
            upstream_model_id=model.upstream_model_id,
        )

        def change(plan):
            request = replace(
                plan.request,
                provider_id=model.provider_id,
                model_id=model.id,
                adapter_kind=model.adapter_kind,
                upstream_model_id=model.upstream_model_id,
            )
            return replace(plan, request=request, input_hash=input_fingerprint(request))

        intent = replace(
            intent,
            generation_plan=change(intent.generation_plan),
            summary_plan=change(intent.summary_plan),
        )
        _, _, checkpoint = await context(ledger)._summarize(
            ledger.run,
            model,
            CachedProvider(),
            intent.summary_plan,
            "key",
            "user",
            1,
            intent=intent,
        )
        assert checkpoint.draft.pricing_context_tokens == 1600
        assert ledger.calls[0].accounted_total_tokens == 1610

    asyncio.run(exercise())


def test_saved_native_actual_highwater_applies_to_summary_billing(tmp_path):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        ledger.run = replace(ledger.run, max_total_tokens=1000000)
        intent = intent_for(ledger)
        provider = Provider()
        await context(ledger)._compact(
            ledger.run, MODEL, provider, native_plan(intent), "key", "user", 1, intent=intent
        )
        saved = ledger.calls[0]
        response = dict(saved.response_snapshot or {})
        billing = json.loads(json.dumps(response["usage_context"]))
        iterations = billing["iterations"]
        iterations[0]["usage"]["input_tokens"] = 300000
        response["usage_context"] = billing
        ledger.calls[0] = replace(
            saved, response_snapshot=response, input_tokens=300000, accounted_total_tokens=300010
        )
        await resume(context(ledger), ledger, provider, ledger.calls[0])
        summary = ledger.calls[1]
        assert summary.response_snapshot is not None
        billing = cast(dict[str, int], summary.response_snapshot["usage_context"])
        assert billing["pricing_context_tokens"] >= 300000
        assert summary.estimated_cost == pytest.approx(0.00145)

    asyncio.run(exercise())


def test_compaction_prefix_cannot_split_a_saved_conversation_turn(tmp_path):
    async def exercise():
        ledger = FileLedger(tmp_path / "ledger.json")
        intent = intent_for(ledger)
        prefix = intent.prefix[:1]
        request = build_summary_request(intent.generation_plan.request, prefix)
        sources = (ContextItem(prefix[0].source_id, ledger.run.turn_id, request.messages[0], True),)
        summary = preflight_context(
            request,
            items=sources,
            profile=PROFILE,
            remaining_run_tokens=ledger.run.max_total_tokens,
            counted_input=InputTokenCount(100, "provider", input_fingerprint(request)),
            remaining_run_cost_usd=ledger.run.max_cost_usd,
        )
        raw = intent.snapshot()
        raw.update(
            {
                "prefix_length": 1,
                "sources": cast(list[object], raw["sources"])[:1],
                "source_hash": _hash([asdict(item) for item in prefix]),
                "summary_plan": plan_snapshot(summary),
            }
        )
        call = await ledger.create_model_call(
            ledger.run.id,
            1,
            MODEL,
            {
                **request_snapshot(summary.request),
                "purpose": "summary",
                "context": summary.snapshot(),
                "compaction_intent": raw,
            },
        )
        provider = Provider()
        with pytest.raises(ApplicationError, match="compaction intent is invalid"):
            await resume(context(ledger), ledger, provider, call)
        assert provider.native_calls == provider.summary_calls == 0

    asyncio.run(exercise())
