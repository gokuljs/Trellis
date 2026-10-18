"""Optional real PostgreSQL adapter checks; run against a disposable migrated DB."""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.application.errors import ApplicationError
from app.domain.context import CheckpointSource, ContextCheckpointDraft
from app.domain.runtime import ModelCallStatus, RunEventType
from app.infrastructure.worker_repository import PostgresWorkerQueue

_ADMIN_DSN = os.environ.get("TRELLIS_WORKER_TEST_ADMIN_DSN")
_WORKER_DSN = os.environ.get("TRELLIS_WORKER_TEST_DSN")


@pytest.mark.skipif(
    not _ADMIN_DSN or not _WORKER_DSN, reason="disposable worker PostgreSQL not configured"
)
def test_real_worker_adapter_executes_without_browser_credentials() -> None:
    assert _ADMIN_DSN and _WORKER_DSN

    async def exercise() -> None:
        assert _ADMIN_DSN and _WORKER_DSN
        async with await psycopg.AsyncConnection.connect(_ADMIN_DSN, autocommit=True) as admin:
            await admin.execute("TRUNCATE trellis_private.jobs CASCADE")
            owner = str(uuid4())
            await admin.execute("INSERT INTO auth.users(id) VALUES(%s)", (owner,))
            await admin.execute("SET ROLE authenticated")
            await admin.execute("SELECT set_config('request.jwt.claim.sub',%s,false)", (owner,))
            await admin.execute("SELECT trellis.ensure_account()")
            row = await (
                await admin.execute("SELECT trellis.mutate('create_session','{}')")
            ).fetchone()
            assert row
            session_id = row[0]["id"]
            row = await (
                await admin.execute(
                    "SELECT trellis.accept_run(%s)",
                    (
                        Jsonb(
                            {
                                "session_id": session_id,
                                "client_request_id": str(uuid4()),
                                "content": "hello",
                                "model_id": "openai:gpt-5.5",
                                "budget_preset": "conservative",
                                "submission": {"content": "hello"},
                            }
                        ),
                    ),
                )
            ).fetchone()
            assert row
            run_id = row[0]["id"]
            await admin.execute("RESET ROLE")
            # No authenticated browser connection remains when worker operations start.
            await admin.execute("SELECT set_config('request.jwt.claim.sub','',false)")
        worker = await PostgresWorkerQueue.open(_WORKER_DSN, max_size=2)
        try:
            assignment = await worker.claim("adapter-test", str(uuid4()), "x" * 40)
            assert assignment and assignment.run_id == run_id
            repository = worker.bind(assignment)
            with pytest.raises(ApplicationError) as denied:
                await repository.assert_provider_access("openai")
            assert denied.value.code == "run_not_owned"
            begun = await worker.begin(assignment)
            assert begun.deadline_at
            assert await worker.begin(assignment) == begun
            context = await repository.get_execution_context()
            assert context.owner_id == owner and context.session.id == session_id
            assert await repository.assert_provider_access("openai") == owner
            with pytest.raises(ApplicationError):
                await repository.assert_provider_access("anthropic")
            assert await repository.get_run(run_id) == begun
            assert await repository.get_session(session_id) == context.session
            assert (await repository.list_sessions())[0].id == session_id
            latest = await repository.get_latest_run_for_session(session_id)
            assert latest is not None and latest.id == run_id
            assert len(await repository.list_runs_for_session(session_id, 0, 10)) == 1
            assert await repository.list_runs_for_session(session_id, 1, 10) == []
            assert (await repository.list_messages(session_id))[0].content == "hello"
            current = (await repository.get_turn_messages(session_id, begun.turn_id))[0]
            assert await repository.get_message(session_id, current.id) == current
            assert (await repository.load_context_window(session_id)).checkpoint is None
            assert await repository.load_context_checkpoint(session_id) is None
            assert (await repository.list_context_messages(session_id, 0))[0] == current
            assert await repository.list_run_messages(run_id) == []
            assert await repository.list_tool_calls(run_id) == []
            assert await repository.list_assistant_outputs(session_id) == []
            with pytest.raises(ApplicationError):
                await repository.get_run(str(uuid4()))
            with pytest.raises(ApplicationError):
                await repository.get_session(str(uuid4()))
            with pytest.raises(PermissionError):
                await repository.create_session()
            with pytest.raises(PermissionError):
                await repository.create_run(session_id, "turn", "request", "input", context.model)
            assert (await worker.renew(assignment)).lease_expires_at
            event = await repository.append_run_event(
                run_id, RunEventType.ASSISTANT_DELTA, {"text": ""}
            )
            assert event.sequence == 3
            call = await repository.create_model_call(run_id, 1, context.model, {})
            assert (await repository.list_model_calls(run_id))[0].id == call.id
            await repository.update_model_call(call.id, ModelCallStatus.STREAMING)
            output, delta = await repository.append_assistant_batch(run_id, call.id, 0, "hello")
            assert output.content == "hello" and delta.sequence == 4
            assert (await repository.list_assistant_outputs(session_id))[0].content == "hello"
            await repository.update_model_call(call.id, ModelCallStatus.COMPLETED)
            # An unrelated or invalid checkpoint cannot cross the assignment boundary.
            draft = ContextCheckpointDraft(
                str(uuid4()),
                str(uuid4()),
                (CheckpointSource(current.id, "message", begun.turn_id),),
                "summary",
                "0" * 64,
                "0" * 64,
                "0" * 64,
                "0" * 64,
                10,
                5,
                None,
                None,
            )
            with pytest.raises(ApplicationError):
                await repository.save_context_checkpoint(run_id, 0, draft)
            events = await repository.list_run_events(run_id)
            assert [value.sequence for value in events] == list(range(1, events[-1].sequence + 1))
            finished_output = await repository.finish_assistant_output(
                run_id, call.id, stop_reason="final_response"
            )
            assert finished_output is not None and finished_output.content == "hello"
            message, completed = await repository.complete_run(run_id, "hello")
            assert message.content == "hello" and completed[-1].event_type == RunEventType.COMPLETED
            with pytest.raises(ApplicationError):
                await repository.assert_provider_access("openai")
        finally:
            await worker.close()
        with pytest.raises(ValueError, match="restricted worker login"):
            await PostgresWorkerQueue.open(_ADMIN_DSN)

    asyncio.run(exercise())
