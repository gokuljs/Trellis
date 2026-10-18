"""Cancellation and expiry policy exercised through separate database identities."""

from __future__ import annotations

import json
from uuid import uuid4

from worker_claims import accept, claim, literal, query
from worker_publication import mutation


def expire(assignment: dict[str, object]) -> None:
    query(f"UPDATE trellis.runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
          f"WHERE id='{assignment['run_id']}';"
          "UPDATE trellis_private.jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
          f"WHERE id='{assignment['job_id']}';"
          "UPDATE trellis_private.execution_attempts SET lease_expires_at=clock_timestamp()-interval '1 second' "
          f"WHERE id='{assignment['attempt_id']}';")


def cancel(run: str, owner: str, *, fails: bool = False) -> dict[str, object] | None:
    result = query(f"SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','{owner}',false);"
                   f"SELECT trellis.mutate('request_run_cancellation',{literal({'run_id': run})});", fails=fails)
    return None if fails else json.loads(result.splitlines()[-1])


def main() -> None:
    query("TRUNCATE trellis_private.jobs CASCADE;")
    run = accept()
    assigned = claim()
    assert assigned is not None
    expire(assigned)
    query(f"SELECT trellis_private.worker_renew({literal(assigned)});", worker=True, fails=True)
    query(f"SELECT trellis_private.worker_read({literal(assigned)},'context');", worker=True, fails=True)
    assert query("SELECT trellis_private.worker_recover_expired(1);", worker=True) == "1"
    assert query(f"SELECT status FROM trellis.runs WHERE id='{run}';") == "queued"
    replacement = claim()
    assert replacement is not None and replacement["attempt_id"] != assigned["attempt_id"]
    query(f"SELECT trellis_private.worker_begin({literal(assigned)});", worker=True, fails=True)
    query(f"SELECT trellis_private.worker_begin({literal(replacement)});", worker=True)
    mutation(replacement, "create_model_call", {"run_id": run, "step_index": 1,
             "model_id": "openai:gpt-5.5", "request_snapshot": {}})
    call_id = query(f"SELECT id FROM trellis.model_calls WHERE run_id='{run}';")
    mutation(replacement, "update_model_call", {"call_id": call_id, "next_status": "streaming"})
    mutation(replacement, "append_assistant_batch", {"run_id": run, "model_call_id": call_id,
             "batch_index": 0, "text": "partial"})
    expire(replacement)
    mutation(replacement, "save_context_checkpoint", {"run_id": run}, fails=True)
    assert query("SELECT trellis_private.worker_recover_expired();", worker=True) == "1"
    assert query(f"SELECT status FROM trellis.runs WHERE id='{run}';") == "interrupted"
    assert query(f"SELECT content||':'||status FROM trellis.run_assistant_outputs WHERE run_id='{run}';") == "partial:partial"
    assert claim() is None
    queued = accept()
    owner = query(f"SELECT user_id FROM trellis.runs WHERE id='{queued}';")
    cancel(queued, str(uuid4()), fails=True)
    result = cancel(queued, owner)
    assert result is not None and result["run"]["status"] == "cancelled"
    assert claim() is None
    running = accept()
    assigned = claim()
    assert assigned is not None
    query(f"SELECT trellis_private.worker_release_unstarted({literal(assigned)});", worker=True)
    query(f"SELECT trellis_private.worker_release_unstarted({literal(assigned)});", worker=True)
    assigned = claim()
    assert assigned is not None
    query(f"SELECT trellis_private.worker_begin({literal(assigned)});", worker=True)
    owner = query(f"SELECT user_id FROM trellis.runs WHERE id='{running}';")
    result = cancel(running, owner)
    assert result is not None and result["run"]["status"] == "cancelling"
    query(f"SELECT trellis_private.worker_renew({literal(assigned)});", worker=True)
    mutation(assigned, "transition_run_record", {"run_id": running, "next_status": "cancelled",
             "event_type": "run.cancelled", "data": {"status": "cancelled"}})
    assert query(f"SELECT status FROM trellis_private.jobs WHERE run_id='{running}';") == "finished"
    query(f"SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','{owner}',false);"
          f"SELECT trellis.mutate('recover_expired_run',{literal({'run_id': running})});", fails=True)
    expiring_cancel = accept()
    assigned = claim()
    assert assigned is not None
    query(f"SELECT trellis_private.worker_begin({literal(assigned)});", worker=True)
    owner = query(f"SELECT user_id FROM trellis.runs WHERE id='{expiring_cancel}';")
    cancel(expiring_cancel, owner)
    expire(assigned)
    assert query("SELECT trellis_private.worker_recover_expired();", worker=True) == "1"
    assert query(f"SELECT status FROM trellis.runs WHERE id='{expiring_cancel}';") == "cancelled"
    assert query(f"SELECT outcome FROM trellis_private.execution_attempts WHERE run_id='{expiring_cancel}';") == "cancelled"
    print("PASS queued reclaim, running interruption, partial output, stale fencing, owner cancellation, and idempotent release")


if __name__ == "__main__":
    main()
