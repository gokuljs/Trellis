from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from run_fixtures import model_record, run_record

from app.domain.recovery import (
    RecoveryAction,
    RecoveryCompatibility,
    RecoveryContract,
    RecoverySnapshot,
    WorkspaceRecoveryReference,
    resolve_recovery,
)
from app.domain.runtime import (
    ModelCallStatus,
    ModelToolCall,
    RunEventType,
    RunMessageRecord,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
    validate_run_event_transition,
)

NOW = datetime.now(UTC)
INSTALLATION = str(uuid4())
WORKSPACE = WorkspaceRecoveryReference(str(uuid4()), 1, "/trusted/project", 2, 3)
COMPATIBILITY = RecoveryCompatibility(
    "checkpoint-runtime-v1", (("responses", "v1"),), (("read_file", "v1"),), INSTALLATION, WORKSPACE
)
CONTRACT = RecoveryContract(
    1,
    COMPATIBILITY.runtime_version,
    COMPATIBILITY.adapter_versions,
    COMPATIBILITY.tool_versions,
    "request",
    INSTALLATION,
    WORKSPACE,
)


def snapshot(**kwargs: object) -> RecoverySnapshot:
    return replace(
        RecoverySnapshot(
            replace(
                run_record(RunStatus.RUNNING),
                recovery_contract=CONTRACT,
                deadline_at=(NOW + timedelta(minutes=5)).isoformat(),
            )
        ),
        **kwargs,
    )


def test_recovery_contract_round_trip_and_immutability() -> None:
    assert RecoveryContract.from_dict(CONTRACT.to_dict()) == CONTRACT
    assert CONTRACT.to_dict()["version"] == 1
    attribute = "runtime_version"
    with pytest.raises(FrozenInstanceError):
        setattr(CONTRACT, attribute, "changed")


@pytest.mark.parametrize("field", ["version", "runtime_version", "installation_id", "workspace"])
def test_recovery_contract_rejects_missing_identity(field: str) -> None:
    value = CONTRACT.to_dict()
    del value[field]
    with pytest.raises(ValueError):
        RecoveryContract.from_dict(value)


def test_legacy_and_terminal_runs_never_recover() -> None:
    legacy = replace(snapshot(), run=run_record())
    assert resolve_recovery(legacy, COMPATIBILITY, NOW).reason == "legacy_run"
    completed = replace(snapshot(), run=replace(snapshot().run, status=RunStatus.COMPLETED))
    assert resolve_recovery(completed, COMPATIBILITY, NOW).action is RecoveryAction.STOP
    assert resolve_recovery(completed, COMPATIBILITY, NOW).reason == "terminal_run"


def test_cancellation_precedes_recovery_and_compatibility() -> None:
    state = snapshot()
    state = replace(
        state,
        run=replace(state.run, cancel_requested_at=NOW.isoformat(), recovery_contract=None),
    )
    assert resolve_recovery(state, COMPATIBILITY, NOW).reason == "cancellation_requested"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"runtime_version": "v2"}, "incompatible_runtime"),
        ({"adapter_versions": (("responses", "v2"),)}, "incompatible_adapter"),
        ({"tool_versions": (("read_file", "v2"),)}, "incompatible_tool"),
        ({"installation_id": str(uuid4())}, "different_installation"),
        ({"workspace": None}, "workspace_unavailable"),
        ({"workspace": replace(WORKSPACE, directory_inode=99)}, "workspace_changed"),
    ],
)
def test_recovery_rejects_incompatible_execution(change: dict, reason: str) -> None:
    assert resolve_recovery(snapshot(), replace(COMPATIBILITY, **change), NOW).reason == reason


def test_prepared_model_request_can_dispatch_without_new_attempt() -> None:
    call = replace(model_record(), request_snapshot={"purpose": "generation"})
    decision = resolve_recovery(snapshot(model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.DISPATCH_MODEL
    assert decision.model_call_id == call.id
    assert decision.usage_unknown is False


def test_dispatched_model_is_preserved_and_never_repeated() -> None:
    call = replace(
        model_record(), status=ModelCallStatus.STREAMING, request_snapshot={"purpose": "summary"}
    )
    decision = resolve_recovery(snapshot(model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.STOP
    assert decision.reason == "model_outcome_unknown"
    assert decision.usage_unknown is True
    assert decision.model_call_id == call.id


def test_completed_summary_without_checkpoint_is_inconsistent() -> None:
    call = replace(
        model_record(),
        status=ModelCallStatus.COMPLETED,
        request_snapshot={"purpose": "summary"},
        response_snapshot={"content": "saved summary"},
    )
    decision = resolve_recovery(snapshot(model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.reason == "inconsistent_execution_records"


def test_completed_native_result_continues_compaction_instead_of_answering() -> None:
    call = replace(
        model_record(),
        status=ModelCallStatus.COMPLETED,
        request_snapshot={"purpose": "native_compaction", "compaction_intent": {"id": "intent"}},
        response_snapshot={"payload_json": "[]"},
    )
    decision = resolve_recovery(snapshot(model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.CONTINUE_COMPACTION
    assert decision.model_call_id == call.id


def test_original_deadline_and_takeover_limit_are_not_reset() -> None:
    state = snapshot()
    expired = replace(state, run=replace(state.run, deadline_at=NOW.isoformat()))
    limited = replace(state, run=replace(state.run, recovery_count=4))
    assert resolve_recovery(expired, COMPATIBILITY, NOW).reason == "deadline_exceeded"
    assert resolve_recovery(limited, COMPATIBILITY, NOW).reason == "recovery_limit_exceeded"


def test_ownership_takeover_has_an_explicit_nonterminal_transition() -> None:
    for status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL):
        validate_run_event_transition(status, RunStatus.RECOVERING, RunEventType.RECOVERING)
    validate_run_event_transition(RunStatus.RECOVERING, RunStatus.RUNNING, RunEventType.RESUMED)
    with pytest.raises(ValueError, match="invalid run transition"):
        validate_run_event_transition(
            RunStatus.INTERRUPTED, RunStatus.RECOVERING, RunEventType.RECOVERING
        )


def tool_snapshot(
    policy: str = "read_only", status: ToolCallStatus = ToolCallStatus.PENDING
) -> RecoverySnapshot:
    call = replace(
        model_record(),
        status=ModelCallStatus.COMPLETED,
        request_snapshot={"purpose": "generation"},
        response_snapshot={
            "content": "Reading the file",
            "tool_calls": [
                {"id": "provider-call", "name": "read_file", "arguments": {"path": "file.txt"}}
            ],
        },
    )
    assistant = RunMessageRecord(
        "assistant",
        call.run_id,
        1,
        "assistant",
        "Reading the file",
        call.id,
        None,
        "now",
        (ModelToolCall("provider-call", "read_file", {"path": "file.txt"}),),
    )
    tool = ToolCallRecord(
        "tool",
        call.run_id,
        assistant.id,
        0,
        "provider-call",
        "read_file",
        {"path": "file.txt"},
        status,
        None,
        None,
        "now",
        None,
        dispatch_attempt=1 if status is ToolCallStatus.RUNNING else 0,
        dispatched_at="now" if status is ToolCallStatus.RUNNING else None,
        recovery_policy=policy,
    )
    return snapshot(model_calls=(call,), run_messages=(assistant,), tool_calls=(tool,))


@pytest.mark.parametrize(
    ("policy", "action"),
    [
        ("read_only", RecoveryAction.EXECUTE_TOOL),
        ("patch", RecoveryAction.AWAIT_APPROVAL),
        ("command", RecoveryAction.AWAIT_APPROVAL),
    ],
)
def test_prepared_tools_preserve_approval_requirements(policy: str, action: RecoveryAction) -> None:
    decision = resolve_recovery(tool_snapshot(policy), COMPATIBILITY, NOW)
    assert decision.action is action
    assert decision.tool_call_id == "tool"


def test_approved_and_denied_tools_reuse_saved_decision() -> None:
    state = tool_snapshot("patch")
    for approval, action in (
        (ToolApprovalDecision.APPROVED, RecoveryAction.EXECUTE_TOOL),
        (ToolApprovalDecision.DENIED, RecoveryAction.RECORD_DENIED_TOOL),
    ):
        tool = replace(state.tool_calls[0], approval_decision=approval)
        decision = resolve_recovery(replace(state, tool_calls=(tool,)), COMPATIBILITY, NOW)
        assert decision.action is action


def test_dispatched_reads_have_only_one_bounded_retry() -> None:
    state = tool_snapshot(status=ToolCallStatus.RUNNING)
    decision = resolve_recovery(state, COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.RETRY_READ_TOOL
    twice = replace(state.tool_calls[0], dispatch_attempt=2)
    decision = resolve_recovery(replace(state, tool_calls=(twice,)), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.STOP
    assert decision.reason == "read_retry_exhausted"


def test_dispatched_patch_requires_reconciliation_before_execution() -> None:
    decision = resolve_recovery(tool_snapshot("patch", ToolCallStatus.RUNNING), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.RECONCILE_PATCH
    assert decision.tool_call_id == "tool"


@pytest.mark.parametrize("policy", ["command", "never", "unknown_new_tool"])
def test_uncertain_mutations_require_review_and_block_retry(policy: str) -> None:
    decision = resolve_recovery(tool_snapshot(policy, ToolCallStatus.RUNNING), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.STOP
    assert decision.reason == "tool_outcome_unknown"
    assert decision.retry_allowed is False
    assert decision.tool_call_id == "tool"


def test_outcome_unknown_is_never_erased_by_automatic_recovery() -> None:
    decision = resolve_recovery(
        tool_snapshot("command", ToolCallStatus.OUTCOME_UNKNOWN), COMPATIBILITY, NOW
    )
    assert decision.action is RecoveryAction.STOP
    assert decision.retry_allowed is False


def test_saved_tool_results_are_reused_before_preparing_next_model() -> None:
    state = tool_snapshot()
    tool = replace(state.tool_calls[0], status=ToolCallStatus.COMPLETED)
    result = RunMessageRecord(
        "result", tool.run_id, 2, "tool", "saved result", None, tool.id, "now"
    )
    decision = resolve_recovery(
        replace(state, tool_calls=(tool,), run_messages=(*state.run_messages, result)),
        COMPATIBILITY,
        NOW,
    )
    assert decision.action is RecoveryAction.PREPARE_NEXT_MODEL
    assert decision.reason == "saved_model_and_tool_results"


@pytest.mark.parametrize(
    "corruption", ["duplicate_id", "cross_run", "missing_assistant", "missing_result"]
)
def test_inconsistent_execution_graph_stops_before_dispatch(corruption: str) -> None:
    state = tool_snapshot()
    if corruption == "duplicate_id":
        state = replace(state, tool_calls=(*state.tool_calls, state.tool_calls[0]))
    elif corruption == "cross_run":
        state = replace(state, model_calls=(replace(state.model_calls[0], run_id="another-run"),))
    elif corruption == "missing_assistant":
        state = replace(state, run_messages=())
    else:
        state = replace(
            state, tool_calls=(replace(state.tool_calls[0], status=ToolCallStatus.COMPLETED),)
        )
    decision = resolve_recovery(state, COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.STOP
    assert decision.reason == "inconsistent_execution_records"


@pytest.mark.parametrize("value", [True, 0, -1])
def test_contract_rejects_invalid_versions(value: object) -> None:
    encoded = CONTRACT.to_dict()
    encoded["version"] = value
    with pytest.raises(ValueError):
        RecoveryContract.from_dict(encoded)


def test_recovery_contract_rejects_duplicate_or_mutable_component_versions() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        replace(CONTRACT, adapter_versions=(("responses", "v1"), ("responses", "v2")))
    encoded = CONTRACT.to_dict()
    encoded["tool_versions"] = {"read_file": False}
    with pytest.raises(ValueError, match="version"):
        RecoveryContract.from_dict(encoded)


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_first_three_claim_generations_are_recoverable(count: int) -> None:
    state = snapshot()
    decision = resolve_recovery(
        replace(state, run=replace(state.run, recovery_count=count)), COMPATIBILITY, NOW
    )
    assert decision.action is RecoveryAction.PREPARE_NEXT_MODEL


def test_active_completed_final_answer_is_rejected_as_inconsistent() -> None:
    state = tool_snapshot()
    call = replace(
        state.model_calls[0],
        response_snapshot={"content": "Final", "tool_calls": [], "finish_kind": "final"},
    )
    decision = resolve_recovery(replace(state, model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.reason == "inconsistent_execution_records"


def test_saved_native_failure_with_known_usage_continues_original_compaction() -> None:
    call = replace(
        model_record(),
        status=ModelCallStatus.FAILED,
        request_snapshot={"purpose": "native_compaction", "compaction_intent": {"id": "intent"}},
        response_snapshot={"native_kind": "openai_window", "native_payload_json": None},
        error_code="native_context_invalid",
        accounted_total_tokens=100,
        estimated_cost=0.01,
    )
    decision = resolve_recovery(snapshot(model_calls=(call,)), COMPATIBILITY, NOW)
    assert decision.action is RecoveryAction.CONTINUE_COMPACTION
    assert decision.model_call_id == call.id


def test_model_cannot_be_prepared_before_previous_tools_finish() -> None:
    state = tool_snapshot()
    following = replace(
        model_record(), id="next-model", step_index=2, request_snapshot={"purpose": "generation"}
    )
    decision = resolve_recovery(
        replace(state, model_calls=(*state.model_calls, following)), COMPATIBILITY, NOW
    )
    assert decision.reason == "inconsistent_execution_records"


def test_later_command_cannot_be_dispatched_before_an_earlier_pending_tool() -> None:
    state = tool_snapshot()
    command = replace(
        state.tool_calls[0],
        id="command",
        call_index=1,
        provider_call_id="command-provider",
        name="run_command",
        status=ToolCallStatus.RUNNING,
        dispatch_attempt=1,
        dispatched_at="now",
        recovery_policy="command",
    )
    declarations = (
        *state.run_messages[0].tool_calls,
        ModelToolCall(command.provider_call_id, command.name, command.arguments),
    )
    assistant = replace(state.run_messages[0], tool_calls=declarations)
    model = replace(
        state.model_calls[0],
        response_snapshot={
            "content": assistant.content,
            "tool_calls": [
                {"id": item.id, "name": item.name, "arguments": item.arguments}
                for item in declarations
            ],
        },
    )
    decision = resolve_recovery(
        replace(
            state,
            model_calls=(model,),
            run_messages=(assistant,),
            tool_calls=(*state.tool_calls, command),
        ),
        COMPATIBILITY,
        NOW,
    )
    assert decision.reason == "inconsistent_execution_records"
    assert decision.retry_allowed is False
