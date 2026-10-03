import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.runtime import ModelCallStatus, RunEvent, RunEventType, RunSnapshot, RunStatus
from app.infrastructure.database import Database


def make_settings(data_dir: Path) -> Settings:
    return Settings(environment="test", data_dir=data_dir)


def test_run_creation_is_idempotent_and_rejects_changed_input(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create() -> tuple[RunSnapshot, RunSnapshot]:
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        first = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "  hello  ",
            model,
        )
        duplicate = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            model,
        )
        return first, duplicate

    first, duplicate = asyncio.run(create())

    assert first.id == duplicate.id
    assert first.status is RunStatus.QUEUED
    assert first.last_event_sequence == 1
    with closing(sqlite3.connect(database.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)

    async def conflicting_input() -> None:
        await database.create_run(
            first.session_id,
            first.turn_id,
            "request-1",
            "different input",
            (await database.list_models())[0],
        )

    with pytest.raises(ValueError, match="run request conflicts"):
        asyncio.run(conflicting_input())


def test_failed_run_can_be_retried_with_a_new_request_id_and_same_user_message(
    tmp_path: Path,
) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create_retry() -> tuple[RunSnapshot, RunSnapshot, int]:
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        first = await database.create_run(
            session.id,
            "turn-retry",
            "request-attempt-1",
            "retry this",
            model,
        )
        await database.transition_run_record(
            first.id,
            RunStatus.FAILED,
            RunEventType.FAILED,
            {"code": "provider_timeout"},
        )
        retry = await database.create_run(
            session.id,
            "turn-retry",
            "request-attempt-2",
            "retry this",
            model,
        )
        messages = await database.list_messages(session.id)
        return first, retry, len(messages)

    first, retry, message_count = asyncio.run(create_retry())
    assert retry.id != first.id
    assert retry.retry_of == first.id
    assert retry.status is RunStatus.QUEUED
    assert message_count == 1


def test_first_run_updates_the_session_title(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create() -> str:
        await database.initialize()
        session = await database.create_session()
        await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "  hello    from   Trellis  ",
            (await database.list_models())[0],
        )
        return session.id

    session_id = asyncio.run(create())
    session = asyncio.run(database.get_session(session_id))

    assert session is not None
    assert session.title == "hello from Trellis"


def test_only_one_active_run_can_own_a_session(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create_runs() -> None:
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        await database.create_run(session.id, "turn-1", "request-1", "one", model)
        await database.create_run(session.id, "turn-2", "request-2", "two", model)

    with pytest.raises(ValueError, match="active run already exists"):
        asyncio.run(create_runs())


def test_idempotent_run_rejects_changed_model_snapshot(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create_and_retry() -> None:
        await database.initialize()
        session = await database.create_session()
        models = await database.list_models()
        await database.create_run(session.id, "turn-1", "request-1", "hello", models[0])
        await database.create_run(session.id, "turn-1", "request-1", "hello", models[1])

    with pytest.raises(ValueError, match="run request conflicts"):
        asyncio.run(create_and_retry())


def test_run_events_are_sequenced_and_replay_after_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)

    async def write_events() -> str:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            (await database.list_models())[0],
        )
        delta = await database.append_run_event(
            run.id,
            RunEventType.ASSISTANT_DELTA,
            {"text": "hello"},
        )
        usage = await database.append_run_event(
            run.id,
            RunEventType.MODEL_USAGE,
            {"input_tokens": 3, "output_tokens": 1},
        )
        assert (delta.sequence, usage.sequence) == (2, 3)
        return run.id

    run_id = asyncio.run(write_events())
    restarted = Database(settings.database_path)
    asyncio.run(restarted.initialize())

    async def replay():
        return await restarted.get_run(run_id), await restarted.list_run_events(run_id, 1)

    run, events = asyncio.run(replay())

    assert run is not None
    assert run.last_event_sequence == 3
    assert [event.sequence for event in events] == [2, 3]
    assert [event.event_type for event in events] == [
        RunEventType.ASSISTANT_DELTA,
        RunEventType.MODEL_USAGE,
    ]
    assert events[0].data == {"text": "hello"}


def test_restart_recovery_interrupts_orphaned_runs_once(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def recover():
        await database.initialize()
        model = (await database.list_models())[0]
        queued_session = await database.create_session()
        running_session = await database.create_session()
        cancelling_session = await database.create_session()
        queued = await database.create_run(
            queued_session.id, "queued-turn", "queued-request", "queued", model
        )
        running = await database.create_run(
            running_session.id, "running-turn", "running-request", "running", model
        )
        await database.transition_run_record(
            running.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        call = await database.create_model_call(running.id, 1, model, {"messages": []})
        await database.update_model_call(call.id, ModelCallStatus.STREAMING)
        cancelling = await database.create_run(
            cancelling_session.id,
            "cancelling-turn",
            "cancelling-request",
            "cancelling",
            model,
        )
        await database.transition_run_record(
            cancelling.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        await database.request_run_cancellation(cancelling.id)

        recovered = await database.recover_active_runs()
        repeated = await database.recover_active_runs()
        runs = [await database.get_run(run_id) for run_id in (queued.id, running.id, cancelling.id)]
        calls = await database.list_model_calls(running.id)
        return recovered, repeated, runs, calls

    recovered, repeated, runs, calls = asyncio.run(recover())
    assert len(recovered) == 3
    assert repeated == ()
    assert all(run is not None and run.status is RunStatus.INTERRUPTED for run in runs)
    assert all(run is not None and run.recovery_count == 1 for run in runs)
    assert calls[0].status is ModelCallStatus.CANCELLED
    assert [event.event_type for event in recovered] == [RunEventType.INTERRUPTED] * 3
    assert all(event.data["code"] == "runtime_restart" for event in recovered)


def test_run_transitions_atomically_update_snapshot_and_event_log(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def exercise() -> tuple[str, RunEvent, RunEvent]:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            (await database.list_models())[0],
        )
        started = await database.transition_run_record(
            run.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        with pytest.raises(ValueError, match="does not match status transition"):
            await database.transition_run_record(
                run.id,
                RunStatus.FAILED,
                RunEventType.CANCELLED,
                {"status": "failed"},
            )
        running = await database.get_run(run.id)
        assert running is not None
        assert running.status is RunStatus.RUNNING
        assert len(await database.list_run_events(run.id)) == 2
        completed = await database.transition_run_record(
            run.id,
            RunStatus.COMPLETED,
            RunEventType.COMPLETED,
            {"status": "completed"},
            stop_reason="final_response",
        )
        return run.id, started, completed

    run_id, started, completed = asyncio.run(exercise())
    persisted = asyncio.run(database.get_run(run_id))
    events = asyncio.run(database.list_run_events(run_id))

    assert started.sequence == 2
    assert completed.sequence == 3
    assert persisted is not None
    assert persisted.status is RunStatus.COMPLETED
    assert persisted.started_at is not None
    assert persisted.finished_at is not None
    assert persisted.stop_reason == "final_response"
    assert [event.event_type for event in events] == [
        RunEventType.QUEUED,
        RunEventType.STARTED,
        RunEventType.COMPLETED,
    ]
    with pytest.raises(ValueError, match="invalid run transition"):
        asyncio.run(
            database.transition_run_record(
                run_id,
                RunStatus.FAILED,
                RunEventType.FAILED,
                {"error": "late failure"},
            )
        )
    final_state = asyncio.run(database.get_run(run_id))
    assert final_state is not None
    assert final_state.status is RunStatus.COMPLETED
    assert len(asyncio.run(database.list_run_events(run_id))) == 3


def test_lifecycle_events_cannot_be_appended_without_status_transition(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def create_run() -> str:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            (await database.list_models())[0],
        )
        return run.id

    run_id = asyncio.run(create_run())

    with pytest.raises(ValueError, match="must use transition_run_record"):
        asyncio.run(
            database.append_run_event(
                run_id,
                RunEventType.COMPLETED,
                {"status": "completed"},
            )
        )
    persisted = asyncio.run(database.get_run(run_id))
    assert persisted is not None
    assert persisted.status is RunStatus.QUEUED
    assert len(asyncio.run(database.list_run_events(run_id))) == 1


def test_non_lifecycle_events_cannot_be_appended_after_terminal_status(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def complete_run() -> str:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            (await database.list_models())[0],
        )
        await database.transition_run_record(
            run.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        await database.transition_run_record(
            run.id,
            RunStatus.COMPLETED,
            RunEventType.COMPLETED,
            {"status": "completed"},
        )
        return run.id

    run_id = asyncio.run(complete_run())

    with pytest.raises(ValueError, match="terminal run"):
        asyncio.run(
            database.append_run_event(
                run_id,
                RunEventType.ASSISTANT_DELTA,
                {"text": "late output"},
            )
        )
    assert len(asyncio.run(database.list_run_events(run_id))) == 3


def test_run_completion_atomically_persists_assistant_message_and_terminal_events(
    tmp_path: Path,
) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def complete() -> str:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-1",
            "request-1",
            "hello",
            (await database.list_models())[0],
        )
        await database.transition_run_record(
            run.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        assistant_message, completion_events = await database.complete_run(
            run.id,
            "Hello from the model",
        )
        assert assistant_message.role == "assistant"
        assert assistant_message.content == "Hello from the model"
        assert [event.event_type for event in completion_events] == [
            RunEventType.ASSISTANT_COMPLETED,
            RunEventType.COMPLETED,
        ]
        return run.id

    run_id = asyncio.run(complete())
    persisted = asyncio.run(database.get_run(run_id))
    events = asyncio.run(database.list_run_events(run_id))
    with closing(sqlite3.connect(database.path)) as connection:
        message_run = connection.execute(
            "SELECT run_id FROM messages WHERE turn_id = ? AND role = 'assistant'",
            ("turn-1",),
        ).fetchone()

    assert persisted is not None
    assert persisted.status is RunStatus.COMPLETED
    assert persisted.last_event_sequence == 4
    assert message_run == (run_id,)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        RunEventType.QUEUED,
        RunEventType.STARTED,
        RunEventType.ASSISTANT_COMPLETED,
        RunEventType.COMPLETED,
    ]


def test_cancelling_queued_run_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def cancel():
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-cancel-queued",
            "request-cancel-queued",
            "hello",
            (await database.list_models())[0],
        )
        cancelled, event = await database.request_run_cancellation(run.id)
        repeated, repeated_event = await database.request_run_cancellation(run.id)
        return cancelled, event, repeated, repeated_event

    cancelled, event, repeated, repeated_event = asyncio.run(cancel())
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.cancel_requested_at is not None
    assert cancelled.finished_at is not None
    assert event is not None and event.event_type is RunEventType.CANCELLED
    assert event.sequence == 2
    assert repeated == cancelled
    assert repeated_event is None
    assert (
        asyncio.run(Database(make_settings(tmp_path).database_path).list_run_events(cancelled.id))[
            -1
        ]
        == event
    )


def test_cancelling_running_run_records_request_before_terminal_event(tmp_path: Path) -> None:
    database = Database(make_settings(tmp_path).database_path)

    async def cancel():
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "turn-cancel-running",
            "request-cancel-running",
            "hello",
            (await database.list_models())[0],
        )
        await database.transition_run_record(
            run.id,
            RunStatus.RUNNING,
            RunEventType.STARTED,
            {"status": "running"},
        )
        cancelling, request_event = await database.request_run_cancellation(run.id)
        repeated, repeated_event = await database.request_run_cancellation(run.id)
        cancelled_event = await database.transition_run_record(
            run.id,
            RunStatus.CANCELLED,
            RunEventType.CANCELLED,
            {"status": "cancelled"},
            stop_reason="user_cancelled",
        )
        cancelled = await database.get_run(run.id)
        events = await database.list_run_events(run.id)
        return (
            cancelling,
            request_event,
            repeated,
            repeated_event,
            cancelled_event,
            cancelled,
            events,
        )

    cancelling, request_event, repeated, repeated_event, cancelled_event, cancelled, events = (
        asyncio.run(cancel())
    )
    assert cancelling.status is RunStatus.CANCELLING
    assert cancelling.cancel_requested_at is not None
    assert request_event is not None
    assert request_event.event_type is RunEventType.CANCELLATION_REQUESTED
    assert request_event.sequence == 3
    assert repeated == cancelling and repeated_event is None
    assert cancelled_event.sequence == 4
    assert cancelled is not None and cancelled.status is RunStatus.CANCELLED
    assert [event.event_type for event in events][-2:] == [
        RunEventType.CANCELLATION_REQUESTED,
        RunEventType.CANCELLED,
    ]


def test_model_call_snapshots_and_usage_survive_database_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)

    async def write_call() -> tuple[str, str]:
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(session.id, "turn-1", "request-1", "hello", model)
        call = await database.create_model_call(
            run.id,
            1,
            model,
            {"messages": [{"role": "user", "content": "hello"}], "max_output_tokens": 512},
        )
        assert call.status is ModelCallStatus.PENDING
        streaming = await database.update_model_call(call.id, ModelCallStatus.STREAMING)
        assert streaming.finished_at is None
        usage = await database.update_model_call(
            call.id,
            ModelCallStatus.STREAMING,
            input_tokens=5,
            cached_tokens=2,
        )
        assert (usage.input_tokens, usage.cached_tokens) == (5, 2)
        await database.update_model_call(
            call.id,
            ModelCallStatus.COMPLETED,
            response_snapshot={"text": "hi"},
            provider_response_id="response-123",
            finish_reason="stop",
            input_tokens=5,
            output_tokens=2,
            reasoning_tokens=1,
            cached_tokens=0,
            estimated_cost=0.0002,
        )
        return run.id, call.id

    run_id, call_id = asyncio.run(write_call())
    restarted = Database(settings.database_path)
    asyncio.run(restarted.initialize())

    async def read_call():
        return await restarted.list_model_calls(run_id)

    calls = asyncio.run(read_call())
    assert len(calls) == 1
    call = calls[0]
    assert call.id == call_id
    assert call.status is ModelCallStatus.COMPLETED
    assert call.request_snapshot == {
        "messages": [{"role": "user", "content": "hello"}],
        "max_output_tokens": 512,
    }
    assert call.response_snapshot == {"text": "hi"}
    assert call.provider_response_id == "response-123"
    assert call.finish_reason == "stop"
    assert (call.input_tokens, call.output_tokens, call.reasoning_tokens, call.cached_tokens) == (
        5,
        2,
        1,
        0,
    )
    assert call.estimated_cost == 0.0002
    assert call.finished_at is not None
    with pytest.raises(ValueError, match="invalid model call transition"):
        asyncio.run(restarted.update_model_call(call_id, ModelCallStatus.FAILED))
    assert asyncio.run(restarted.list_model_calls(run_id))[0].status is ModelCallStatus.COMPLETED
