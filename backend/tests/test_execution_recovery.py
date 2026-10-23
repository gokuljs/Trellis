import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("recovery_process_fixture.py")
BACKEND = FIXTURE.parents[1]


def process(path: Path, phase: str, fault: str = "", *, dies: bool = False):
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(BACKEND), str(FIXTURE.parent)])}
    child = subprocess.run(
        [sys.executable, str(FIXTURE), str(path), phase, fault],
        cwd=BACKEND,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == (73 if dies else 0), child.stdout + child.stderr
    return json.loads(path.read_text())


def finish(path: Path):
    data = process(path, "recover")
    if data["run"]["status"] == "waiting_for_approval":
        data = process(path, "approve")
    return data


@pytest.mark.parametrize(
    "boundary", ["completed_model", "between_tools", "approval_wait", "prepared_model"]
)
def test_fresh_process_recovers_saved_execution_boundaries(tmp_path, boundary):
    path = tmp_path / "ledger.json"
    before = process(path, "start", boundary, dies=True)
    after = finish(path)
    assert after["run"]["id"] == before["run"]["id"]
    assert after["run"]["status"] == "completed"
    assert after["run"]["generation"] >= 1
    assert after["run"]["deadline_at"] == before["run"]["deadline_at"]
    assert after["run"]["max_total_tokens"] == before["run"]["max_total_tokens"]
    assert after["stats"] == {
        "model_dispatches": 2,
        "read_dispatches": 1,
        "patch_dispatches": 1,
        "command_dispatches": 0,
    }
    assert (tmp_path / "workspace" / "hello.txt").read_text() == "goodbye world\n"
    assert [call["step_index"] for call in after["calls"]] == [1, 2]
    if boundary != "prepared_model":
        assert after["calls"][0]["response_snapshot"] == before["calls"][0]["response_snapshot"]
        assert after["calls"][0]["accounted_total_tokens"] == 110


def test_patch_changed_before_result_save_is_reconciled_without_another_patch(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "changed_patch", dies=True)
    target = tmp_path / "workspace" / "hello.txt"
    identity = target.stat()
    assert target.read_text() == "goodbye world\n"
    assert before["tools"][-1]["status"] == "running"
    after = finish(path)
    assert after["run"]["status"] == "completed"
    assert after["stats"]["patch_dispatches"] == 1
    assert after["stats"]["read_dispatches"] == 1
    assert target.stat() == identity
    patch = after["tools"][-1]
    assert patch["result_source"] == "reconciled_desired_state"
    assert patch["result_snapshot"]["source"] == "reconciled_desired_state"
    proof = patch["result_snapshot"]["reconciliation_proof"]
    assert proof["observed_sha256"] == proof["after_sha256"]
    assert proof["before_sha256"] != proof["after_sha256"]
    assert (
        len([message for message in after["messages"] if message["tool_call_id"] == patch["id"]])
        == 1
    )


def test_conflicting_patch_preserves_unknown_outcome_and_blocks_retry(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "changed_patch", dies=True)
    target = tmp_path / "workspace" / "hello.txt"
    target.write_text("An external change.\n")
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "patch_conflict"
    assert after["run"]["retry_allowed"] is False
    assert after["run"]["uncertain_tool_call_id"] == after["tools"][-1]["id"]
    assert after["tools"][-1]["status"] == "outcome_unknown"
    assert after["stats"]["model_dispatches"] == 1
    assert after["stats"]["patch_dispatches"] == 1
    assert target.read_text() == "An external change.\n"


def test_unknown_model_preserves_partial_output_without_another_paid_dispatch(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "streaming_model", dies=True)
    assert before["outputs"][0]["content"] == "Saved partial answer. "
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "model_outcome_unknown"
    assert after["stats"]["model_dispatches"] == 1
    assert after["outputs"][0]["content"] == before["outputs"][0]["content"]
    assert after["outputs"][0]["status"] == "partial"
    assert after["calls"][0]["estimated_cost"] is None
    assert after["calls"][0]["accounted_total_tokens"] is None


def test_unknown_command_is_never_executed_again(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "unknown_command", dies=True)
    target = tmp_path / "workspace" / "delivered.txt"
    identity = target.stat()
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "tool_outcome_unknown"
    assert after["run"]["retry_allowed"] is False
    assert after["run"]["uncertain_tool_call_id"] == before["tools"][0]["id"]
    assert after["stats"]["command_dispatches"] == 1
    assert after["stats"]["model_dispatches"] == 1
    assert target.stat() == identity
    assert json.loads((tmp_path / "private" / "workspace-operations.json").read_text())["commands"]


def test_final_publication_survives_process_death_without_recovery_payment(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "final_publication", dies=True)
    after = process(path, "recover")
    assert after["run"]["status"] == "completed"
    assert after["run"]["generation"] == 0
    assert after["stats"] == before["stats"]
    assert len([event for event in after["events"] if event["event_type"] == "run.completed"]) == 1


@pytest.mark.parametrize("fault", ["lost_generation_ack", "invalid_generation_ack"])
def test_lost_generation_acknowledgment_resumes_saved_tools_without_repeating_model(
    tmp_path, fault
):
    path = tmp_path / "ledger.json"
    before = process(path, "start", fault)
    assert before["run"]["status"] == "running"
    assert before["calls"][0]["status"] == "completed"
    after = finish(path)
    assert after["run"]["status"] == "completed"
    assert after["stats"]["model_dispatches"] == 2
    assert len(after["calls"]) == 2


def test_dispatched_read_gets_one_retry_of_the_same_logical_operation(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "dispatched_read", dies=True)
    after = finish(path)
    assert after["run"]["status"] == "completed"
    assert after["tools"][0]["id"] == before["tools"][0]["id"]
    assert after["tools"][0]["dispatch_attempt"] == 2
    assert after["stats"]["read_dispatches"] == 1


def test_read_retry_is_bounded_across_multiple_process_deaths(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "dispatched_read", dies=True)
    process(path, "recover", "dispatched_read", dies=True)
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "read_retry_exhausted"
    assert after["tools"][0]["dispatch_attempt"] == 2
    assert after["stats"]["read_dispatches"] == 0
    assert after["stats"]["model_dispatches"] == 1


def test_authorized_read_retry_is_revalidated_after_another_process_dies(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "dispatched_read", dies=True)
    authorized = process(path, "recover", "retry_authorized", dies=True)
    assert authorized["tools"][0]["status"] == "pending"
    assert authorized["tools"][0]["dispatch_attempt"] == 1
    after = finish(path)
    assert after["run"]["status"] == "completed"
    assert after["tools"][0]["dispatch_attempt"] == 2
    assert after["stats"]["read_dispatches"] == 1


def test_saved_approval_is_restored_without_changing_preview(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "approval_wait", dies=True)
    restored = process(path, "recover")
    assert restored["run"]["status"] == "waiting_for_approval"
    assert restored["tools"][-1]["approval_preview"] == before["tools"][-1]["approval_preview"]
    assert restored["tools"][-1]["approval_decision"] is None
    assert restored["stats"]["patch_dispatches"] == 0
    assert (
        len(
            [
                event
                for event in restored["events"]
                if event["event_type"] == "tool.approval_requested"
            ]
        )
        == 1
    )


def test_cancelled_unknown_command_stays_cancelled_without_repetition(tmp_path):
    path = tmp_path / "ledger.json"
    data = process(path, "start", "unknown_command", dies=True)
    data["run"]["status"] = "cancelling"
    data["run"]["cancel_requested_at"] = "2026-10-23T00:00:00+00:00"
    path.write_text(json.dumps(data))
    after = process(path, "recover")
    assert after["run"]["status"] == "cancelled"
    assert after["run"]["retry_allowed"] is False
    assert after["stats"]["command_dispatches"] == 1


def test_unchanged_dispatched_patch_is_retried_under_original_approval(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "dispatched_patch", dies=True)
    assert (tmp_path / "workspace" / "hello.txt").read_text() == "hello world\n"
    after = process(path, "recover")
    assert after["run"]["status"] == "completed"
    assert after["stats"]["patch_dispatches"] == 1
    assert after["tools"][-1]["dispatch_attempt"] == 2
    assert after["tools"][-1]["approval_preview"] == before["tools"][-1]["approval_preview"]
    assert (tmp_path / "workspace" / "hello.txt").read_text() == "goodbye world\n"


def test_previously_authorized_patch_is_reconciled_again_after_process_death(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "dispatched_patch", dies=True)
    authorized = process(path, "recover", "retry_authorized", dies=True)
    assert authorized["tools"][-1]["status"] == "pending"
    target = tmp_path / "workspace" / "hello.txt"
    target.write_text("Changed after retry authorization.\n")
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "patch_conflict"
    assert after["stats"]["patch_dispatches"] == 0
    assert after["run"]["retry_allowed"] is False
    assert target.read_text() == "Changed after retry authorization.\n"


def test_recovery_rejects_changed_workspace_guidance_before_executing_pending_tool(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "completed_model", dies=True)
    (tmp_path / "workspace" / "AGENTS.md").write_text("New instructions.\n")
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "recovery_inputs_changed"
    assert after["stats"]["read_dispatches"] == 0
    assert after["stats"]["patch_dispatches"] == 0
    assert after["stats"]["model_dispatches"] == 1


def test_recovery_rejects_changed_prior_read_observation_before_a_patch(tmp_path):
    path = tmp_path / "ledger.json"
    process(path, "start", "between_tools", dies=True)
    target = tmp_path / "workspace" / "hello.txt"
    target.write_text("Externally changed before approval.\n")
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "workspace_inputs_changed"
    assert after["stats"]["patch_dispatches"] == 0
    assert after["stats"]["model_dispatches"] == 1


def test_revoked_workspace_grant_stops_recovery_without_reading_cloud_path(tmp_path):
    path = tmp_path / "ledger.json"
    data = process(path, "start", "completed_model", dies=True)
    grant_file = (
        tmp_path / "private" / "10000000-0000-4000-8000-000000000001" / "workspace-grants.json"
    )
    grants = json.loads(grant_file.read_text())
    grants["grants"] = []
    grant_file.write_text(json.dumps(grants))
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "workspace_unavailable"
    assert after["run"]["recovery_contract"] == data["run"]["recovery_contract"]
    assert after["stats"]["model_dispatches"] == 1
    assert after["stats"]["read_dispatches"] == 0


def test_incompatible_runtime_preserves_dispatched_patch_uncertainty(tmp_path):
    path = tmp_path / "ledger.json"
    data = process(path, "start", "changed_patch", dies=True)
    data["run"]["recovery_contract"]["runtime_version"] = "another-runtime-version"
    path.write_text(json.dumps(data))
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "incompatible_runtime"
    assert after["run"]["retry_allowed"] is False
    assert after["stats"]["patch_dispatches"] == 1
    assert after["tools"][-1]["status"] == "outcome_unknown"


def test_prepared_model_uses_original_remaining_model_quota(tmp_path):
    path = tmp_path / "ledger.json"
    data = process(path, "start", "prepared_model", dies=True)
    data["run"]["max_model_calls"] = 1
    path.write_text(json.dumps(data))
    after = process(path, "recover")
    assert after["stats"]["model_dispatches"] == 1
    assert len(after["calls"]) == 1
    assert after["calls"][0]["status"] == "completed"
    assert after["run"]["status"] == "failed"
    assert after["stats"]["patch_dispatches"] == 0


def test_graceful_shutdown_preserves_dispatched_model_until_recovery_assesses_it(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "graceful_shutdown")
    assert before["run"]["status"] == "running"
    assert before["calls"][0]["status"] == "streaming"
    assert before["outputs"][0]["content"]
    assert not any(event["event_type"] == "run.interrupted" for event in before["events"])
    after = process(path, "recover")
    assert after["run"]["status"] == "interrupted"
    assert after["run"]["recovery_reason"] == "model_outcome_unknown"
    assert after["stats"]["model_dispatches"] == 1
    assert after["outputs"][0]["content"] == before["outputs"][0]["content"]


def test_queued_shutdown_preserves_never_dispatched_execution(tmp_path):
    path = tmp_path / "ledger.json"
    before = process(path, "start", "queued_shutdown")
    assert before["run"]["status"] == "queued"
    assert before["stats"]["model_dispatches"] == 0
    assert not before["calls"]
    after = finish(path)
    assert after["run"]["status"] == "completed"
    assert after["stats"]["model_dispatches"] == 2
