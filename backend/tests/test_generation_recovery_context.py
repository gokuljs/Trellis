import asyncio
import json
from dataclasses import asdict, replace
from typing import cast
from uuid import uuid4

import pytest
from run_fixtures import MODEL, model_record, run_record

from app.application.context import build_model_context
from app.application.context_budget import input_fingerprint
from app.application.errors import ApplicationError
from app.application.execution_snapshots import generation_snapshot, restore_context_plan
from app.application.model_catalog import ModelProfile, profile_for_model
from app.application.ports import ContextCheckpointRepository, RunRepository, SessionRepository
from app.application.run_context import RunContext, _hash
from app.application.secret_redaction import SecretRedactor
from app.domain.context import (
    CheckpointSource,
    ContextCheckpoint,
    ContextCheckpointDraft,
    ContextWindow,
    InputTokenCount,
)
from app.domain.models import Message
from app.domain.recovery import RecoverySnapshot
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelToolCall,
    ModelToolSpec,
    RunMessageRecord,
    RunStatus,
)

PROFILE = cast(ModelProfile, profile_for_model(MODEL.id))


class Ledger:
    def __init__(self):
        self.run = run_record(RunStatus.RUNNING)
        self.messages: tuple[Message, ...] = (
            Message(
                self.run.input_message_id,
                self.run.session_id,
                self.run.turn_id,
                1,
                "user",
                "Explain the project",
                None,
                None,
                "now",
            ),
        )
        self.calls: list[ModelCallRecord] = []
        self.checkpoint: ContextCheckpoint | None = None

    async def load_context_window(self, session_id):
        return ContextWindow(self.checkpoint, self.messages)

    async def list_model_calls(self, run_id):
        return self.calls.copy()

    async def list_run_messages(self, run_id):
        return []

    async def list_tool_calls(self, run_id):
        return []


class Provider:
    def __init__(self):
        self.counts = 0

    async def count_input(self, request, api_key, user_id):
        self.counts += 1
        return InputTokenCount(100, "provider", input_fingerprint(request))

    async def stream(self, request, api_key, user_id):
        pytest.fail("Generation validation dispatched a paid request")
        yield

    async def compact(self, request, api_key, user_id):
        pytest.fail("Generation validation dispatched compaction")


def runtime(ledger):
    async def publish(event):
        pytest.fail("Generation validation published an execution event")

    return RunContext(
        cast(RunRepository, ledger),
        cast(SessionRepository, ledger),
        cast(ContextCheckpointRepository, ledger),
        lambda model, calls: (
            sum(call.accounted_total_tokens or 0 for call in calls),
            sum(call.estimated_cost or 0 for call in calls),
        ),
        publish,
        SecretRedactor(),
    )


async def prepared(ledger, provider, *, workspace_root=None, guidance=None, tools=()):
    plan = await runtime(ledger).prepare(
        ledger.run,
        MODEL,
        PROFILE,
        provider,
        "key",
        "user",
        workspace_root=workspace_root,
        guidance=guidance,
        tools=tools,
    )
    saved = json.loads(json.dumps(generation_snapshot(plan)))
    call = replace(model_record(), request_snapshot=saved)
    ledger.calls = [call]
    return plan, call


def validate(ledger, provider, call, **kwargs):
    return runtime(ledger).validate_saved_generation(
        ledger.run,
        MODEL,
        PROFILE,
        provider,
        "key",
        "user",
        call,
        workspace_root=kwargs.pop("workspace_root", None),
        guidance=kwargs.pop("guidance", None),
        tools=kwargs.pop("tools", ()),
        **kwargs,
    )


def test_saved_generation_reuses_exact_request_without_allocating_or_generating():
    async def scenario():
        ledger, provider = Ledger(), Provider()
        plan, call = await prepared(ledger, provider)
        restored = await validate(ledger, provider, call)
        assert restored.request == plan.request
        assert restored.sources == plan.sources
        assert ledger.calls == [call]
        assert provider.counts == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["content", "identity", "tools", "workspace", "guidance"])
def test_changed_generation_inputs_stop_before_paid_dispatch(tmp_path, change):
    async def scenario():
        ledger, provider = Ledger(), Provider()
        workspace = tmp_path if change == "guidance" else None
        _plan, call = await prepared(ledger, provider, workspace_root=workspace)
        kwargs = {"workspace_root": workspace}
        if change == "content":
            ledger.messages = (replace(ledger.messages[0], content="Different question"),)
        elif change == "identity":
            ledger.messages = (replace(ledger.messages[0], turn_id="different-turn"),)
        elif change == "tools":
            kwargs["tools"] = (ModelToolSpec("read_file", "Read", {"type": "object"}),)
        elif change == "workspace":
            kwargs["workspace_root"] = tmp_path
        else:
            kwargs["guidance"] = "Changed instructions"
        with pytest.raises(ApplicationError) as error:
            await validate(ledger, provider, call, **kwargs)
        assert error.value.code == "recovery_inputs_changed"
        assert provider.counts == 1

    asyncio.run(scenario())


def test_generation_validation_uses_one_consistent_snapshot():
    async def scenario():
        ledger, provider = Ledger(), Provider()
        plan, call = await prepared(ledger, provider)
        saved = RecoverySnapshot(ledger.run, model_calls=(call,), messages=ledger.messages)
        ledger.messages = ()
        ledger.calls = []
        restored = await validate(ledger, provider, call, snapshot=saved)
        assert restored.request == plan.request

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["missing_plan", "context_hash", "model", "already_dispatched"])
def test_inconsistent_saved_generation_is_rejected(change):
    async def scenario():
        ledger, provider = Ledger(), Provider()
        _plan, call = await prepared(ledger, provider)
        saved = json.loads(json.dumps(call.request_snapshot))
        if change == "missing_plan":
            del saved["generation_plan"]
        elif change == "context_hash":
            saved["context"]["source_hash"] = "0" * 64
        elif change == "model":
            saved["model_id"] = "different-model"
        else:
            call = replace(call, status=ModelCallStatus.STREAMING)
        call = replace(call, request_snapshot=saved)
        with pytest.raises(ApplicationError) as error:
            await validate(ledger, provider, call)
        assert error.value.code == "inconsistent_execution_records"
        assert provider.counts == 1

    asyncio.run(scenario())


def test_recovery_keeps_original_output_reservation_and_budget():
    async def scenario():
        ledger, provider = Ledger(), Provider()
        ledger.run = replace(ledger.run, max_total_tokens=300)
        plan, call = await prepared(ledger, provider)
        assert plan.request.max_output_tokens == 200
        restored = await validate(ledger, provider, call)
        assert restored.request.max_output_tokens == 200
        ledger.run = replace(ledger.run, max_total_tokens=250)
        with pytest.raises(ApplicationError) as error:
            await validate(ledger, provider, call)
        assert error.value.code == "run_budget_exhausted"

    asyncio.run(scenario())


def test_generation_snapshot_rejects_secret_metadata_without_corrupting_saved_hashes():
    async def scenario():
        ledger, provider = Ledger(), Provider()
        plan, _call = await prepared(ledger, provider)
        with pytest.raises(ApplicationError) as error:
            generation_snapshot(plan, redactor=SecretRedactor("Explain the project"))
        assert error.value.code == "recovery_inputs_sensitive"
        assert "Explain the project" not in str(error.value)

    asyncio.run(scenario())


def test_context_plan_rejects_conflicting_source_references():
    async def scenario():
        ledger, provider = Ledger(), Provider()
        _plan, call = await prepared(ledger, provider)
        saved = call.request_snapshot["generation_plan"]
        saved["sources"][0]["source_id"] = "different-source"
        with pytest.raises(ValueError):
            restore_context_plan(saved, ledger.run)

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", [False, True])
def test_tool_recovery_checks_inputs_before_its_original_assistant_message(tmp_path, changed):
    async def scenario():
        ledger, provider = Ledger(), Provider()
        tools = (ModelToolSpec("read_file", "Read", {"type": "object"}),)
        _plan, call = await prepared(
            ledger, provider, workspace_root=tmp_path, guidance="Original guidance", tools=tools
        )
        completed = replace(call, status=ModelCallStatus.COMPLETED)
        assistant = RunMessageRecord(
            "assistant",
            ledger.run.id,
            1,
            "assistant",
            "Reading the file",
            call.id,
            None,
            "now",
            (ModelToolCall("provider-call", "read_file", {"path": "file.txt"}),),
        )
        result = RunMessageRecord(
            "result", ledger.run.id, 2, "tool", "Saved file content", None, "tool-id", "now"
        )
        saved = RecoverySnapshot(
            ledger.run,
            model_calls=(completed,),
            messages=ledger.messages,
            run_messages=(assistant, result),
        )
        kwargs = {
            "workspace_root": tmp_path,
            "guidance": "Changed guidance" if changed else "Original guidance",
            "tools": tools,
            "snapshot": saved,
        }
        if changed:
            with pytest.raises(ApplicationError) as error:
                runtime(ledger).validate_recovery_inputs(ledger.run, completed, **kwargs)
            assert error.value.code == "recovery_inputs_changed"
        else:
            runtime(ledger).validate_recovery_inputs(ledger.run, completed, **kwargs)
        assert provider.counts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
def test_generation_recovery_validates_saved_portable_and_native_context(native):
    async def scenario():
        ledger, provider = Ledger(), Provider()
        context = build_model_context(ledger.messages, (), workspace_root=None, tools=())
        draft = ContextCheckpointDraft(
            str(uuid4()),
            str(uuid4()),
            (CheckpointSource(str(uuid4()), "message", "old-turn"),),
            "A saved summary of earlier work.",
            "a" * 64,
            "b" * 64,
            _hash(
                {
                    "system": context.system_instructions,
                    "tools": [asdict(tool) for tool in context.tools],
                }
            ),
            _hash({"root": None, "guidance": None}),
            100,
            10,
            "openai_window" if native else None,
            '[{"type":"compaction","value":"saved"}]' if native else None,
            100,
        )
        ledger.checkpoint = ContextCheckpoint(
            ledger.run.session_id,
            "previous-run",
            1,
            None,
            ledger.run.provider_id,
            ledger.run.model_id,
            ledger.run.adapter_kind,
            0,
            0,
            "now",
            draft,
        )
        plan, call = await prepared(ledger, provider)
        assert (plan.request.native_context_json is not None) is native
        restored = await validate(ledger, provider, call)
        assert restored.request == plan.request
        assert restored.checkpoint_id == draft.id
        ledger.checkpoint = replace(
            ledger.checkpoint, draft=replace(draft, summary="Changed historical summary")
        )
        if native:
            ledger.checkpoint = replace(
                ledger.checkpoint,
                draft=replace(
                    draft, native_payload_json='[{"type":"compaction","value":"changed"}]'
                ),
            )
        with pytest.raises(ApplicationError) as error:
            await validate(ledger, provider, call)
        assert error.value.code == "recovery_inputs_changed"

    asyncio.run(scenario())
