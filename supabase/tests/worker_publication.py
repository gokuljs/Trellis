"""Fenced, idempotent publication through real restricted worker connections."""

from __future__ import annotations

import json
from uuid import uuid4

from worker_claims import accept, claim, literal, query


def mutation(assignment: dict[str, object], action: str, payload: dict[str, object],
             operation: str | None = None, *, fails: bool = False) -> object:
    value = query(
        f"SELECT trellis_private.worker_mutate({literal(assignment)},'{operation or uuid4()}',"
        f"'{action}',{literal(payload)});", worker=True, fails=fails,
    )
    return None if fails else json.loads(value)


def main() -> None:
    query("TRUNCATE trellis_private.jobs CASCADE;")
    run = accept()
    assignment = claim()
    assert assignment is not None
    query(f"SELECT trellis_private.worker_read({literal(assignment)},'credential',"
          "'{\"provider\":\"openai\"}'::jsonb);", worker=True, fails=True)
    query(f"SELECT trellis_private.worker_begin({literal(assignment)});", worker=True)
    context = json.loads(query(
        f"SELECT trellis_private.worker_read({literal(assignment)},'context');", worker=True,
    ))["result"]
    assert context["run"]["id"] == run
    query(f"SELECT trellis_private.worker_read({literal(assignment)},'credential',"
          "'{\"provider\":\"anthropic\"}'::jsonb);", worker=True, fails=True)
    owner = context["owner_id"]
    query(f"SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','{owner}',false);"
          "SELECT trellis.mutate('append_run_event',"
          f"{literal({'run_id': run, 'event_type': 'assistant.delta', 'data': {}})},"
          f"'{assignment['capability']}');", fails=True)
    operation = str(uuid4())
    payload = {"run_id": run, "event_type": "assistant.delta", "data": {"text": "hello"}}
    result = mutation(assignment, "append_run_event", payload, operation)
    assert result == mutation(assignment, "append_run_event", payload, operation)
    mutation(assignment, "append_run_event", {**payload, "data": {}}, operation, fails=True)
    mutation(assignment, "append_run_event", {**payload, "run_id": str(uuid4())}, fails=True)
    mutation(assignment, "create_session", {}, fails=True)
    call = mutation(assignment, "create_model_call", {"run_id": run, "step_index": 1,
                    "model_id": "openai:gpt-5.5", "request_snapshot": {}})
    assert isinstance(call, dict)
    mutation(assignment, "update_model_call", {"call_id": call["id"], "next_status": "completed",
             "response_snapshot": {}, "input_tokens": 1, "output_tokens": 1})
    operation = str(uuid4())
    completed = mutation(assignment, "complete_run", {"run_id": run, "content": "hello"}, operation)
    assert mutation(assignment, "complete_run", {"run_id": run, "content": "hello"}, operation) == completed
    assert query(f"SELECT status FROM trellis_private.jobs WHERE run_id='{run}';") == "finished"
    assert query(f"SELECT outcome FROM trellis_private.execution_attempts WHERE run_id='{run}';") == "completed"
    query(f"SELECT trellis_private.worker_read({literal(assignment)},'context');", worker=True, fails=True)
    mutation(assignment, "append_run_event", payload, fails=True)
    print("PASS scoped reads, provider fencing, browser denial, ordered writes, deduplication, and atomic completion")


if __name__ == "__main__":
    main()
