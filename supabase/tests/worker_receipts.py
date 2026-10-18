"""Retry journal growth remains linear while old publication results replay exactly."""

from uuid import uuid4

from worker_claims import accept, claim, literal, query
from worker_publication import mutation


def main() -> None:
    query("TRUNCATE trellis_private.jobs CASCADE;")
    run = accept()
    assignment = claim()
    assert assignment is not None
    query(f"SELECT trellis_private.worker_begin({literal(assignment)});", worker=True)
    call = mutation(assignment, "create_model_call", {"run_id": run, "step_index": 1,
                    "model_id": "openai:gpt-5.5", "request_snapshot": {}})
    assert isinstance(call, dict)
    mutation(assignment, "update_model_call", {"call_id": call["id"], "next_status": "streaming"})
    operation = str(uuid4())
    payload = {"run_id": run, "model_call_id": call["id"], "batch_index": 0, "text": "🙂" * 64}
    original = mutation(assignment, "append_assistant_batch", payload, operation)
    for index in range(1, 96):
        mutation(assignment, "append_assistant_batch", {**payload, "batch_index": index})
        if index == 47:
            first_size = int(query(f"SELECT sum(pg_column_size(result)) FROM trellis_private.worker_operations WHERE attempt_id='{assignment['attempt_id']}';"))
    total_size = int(query(f"SELECT sum(pg_column_size(result)) FROM trellis_private.worker_operations WHERE attempt_id='{assignment['attempt_id']}';"))
    assert total_size < 96 * 2304, total_size
    assert total_size - first_size < first_size * 1.2 + 4096, (first_size, total_size)
    assert original == mutation(assignment, "append_assistant_batch", payload, operation)
    finish_operation = str(uuid4())
    finish_payload = {"run_id": run, "model_call_id": call["id"], "stop_reason": "final_response"}
    finished_output = mutation(assignment, "finish_assistant_output", finish_payload, finish_operation)
    for _ in range(8):
        mutation(assignment, "finish_assistant_output", finish_payload)
    assert finished_output == mutation(assignment, "finish_assistant_output", finish_payload, finish_operation)
    mutation(assignment, "update_model_call", {"call_id": call["id"], "next_status": "completed"})
    mutation(assignment, "complete_run", {"run_id": run, "content": "🙂" * (64 * 96)})
    assert original == mutation(assignment, "append_assistant_batch", payload, operation)
    # Repeated usage updates cannot repeat the immutable full request in storage.
    run = accept()
    assignment = claim()
    assert assignment is not None
    query(f"SELECT trellis_private.worker_begin({literal(assignment)});", worker=True)
    call = mutation(assignment, "create_model_call", {"run_id": run, "step_index": 1,
                    "model_id": "openai:gpt-5.5", "request_snapshot": {"history": "x" * 32768}})
    assert isinstance(call, dict)
    operation = str(uuid4())
    update = {"call_id": call["id"], "next_status": "streaming", "input_tokens": 1}
    original = mutation(assignment, "update_model_call", update, operation)
    for count in range(2, 22):
        mutation(assignment, "update_model_call", {**update, "input_tokens": count})
    usage_bytes = int(query(f"SELECT sum(pg_column_size(result)) FROM trellis_private.worker_operations WHERE attempt_id='{assignment['attempt_id']}';"))
    assert usage_bytes < 22 * 2304, usage_bytes
    assert original == mutation(assignment, "update_model_call", update, operation)
    # Fault injection reaches both ordinary limits without a huge test transcript.
    query("UPDATE trellis_private.execution_attempts SET operation_storage_bytes=8388608,"
          f"operation_storage_count=32768 WHERE id='{assignment['attempt_id']}';")
    sequence = query(f"SELECT last_event_sequence FROM trellis.runs WHERE id='{run}';")
    mutation(assignment, "append_run_event", {"run_id": run, "event_type": "assistant.delta", "data": {}}, fails=True)
    assert query(f"SELECT last_event_sequence FROM trellis.runs WHERE id='{run}';") == sequence
    mutation(assignment, "transition_run_record", {"run_id": run, "next_status": "failed",
             "event_type": "run.failed", "data": {"code": "event_storage_limit"}})
    assert query(f"SELECT status FROM trellis_private.jobs WHERE run_id='{run}';") == "finished"
    assert original == mutation(assignment, "update_model_call", update, operation)
    print(f"PASS model request receipts ({usage_bytes} bytes for 21 usage updates), exact replay, rollback, and reserved terminal headroom")
    print(f"PASS linear retry receipts ({total_size} bytes for 96 batches), UTF-8 prefix replay, and exact replay after completion")


if __name__ == "__main__":
    main()
