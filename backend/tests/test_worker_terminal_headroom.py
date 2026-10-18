import asyncio
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.application.errors import ApplicationError
from app.application.execution import RunExecutor
from app.application.worker_ports import WorkerExecutionRepository
from app.domain.runtime import ModelCallStatus, RunEventType, RunStatus
from app.infrastructure.server_credentials import ServerCredentialStore
from app.infrastructure.worker_repository import PostgresWorkerQueue
from tests.memory_repository import MemoryRepository
from tests.support import make_settings


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("code", ["event_storage_limit", "run_not_owned", "database_unavailable"])
def test_terminal_cleanup_uses_headroom_without_hiding_authority_errors(
    tmp_path: Path, outcome: str, code: str
) -> None:
    class JournalRepository(MemoryRepository):
        reject_updates = False

        async def update_model_call(self, call_id, next_status, **kwargs):
            if self.reject_updates:
                raise ApplicationError(code, "Ordinary publication rejected")
            return await super().update_model_call(call_id, next_status, **kwargs)

    async def scenario():
        repository = JournalRepository(tmp_path)
        session = await repository.create_session()
        model = (await repository.list_models())[0]
        run = await repository.create_run(session.id, "turn", "request", "Hello", model)
        await repository.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})
        call = await repository.create_model_call(run.id, 1, model, {})
        call = await repository.update_model_call(call.id, ModelCallStatus.STREAMING)
        await repository.append_assistant_batch(run.id, call.id, 0, "Published partial output")
        if outcome == "cancelled":
            await repository.request_run_cancellation(run.id)
        repository.reject_updates = True
        executor = RunExecutor(
            cast(WorkerExecutionRepository, repository),
            ServerCredentialStore(make_settings(tmp_path)),
            {},
        )

        async def finish():
            if outcome == "failed":
                await executor._fail(run.id, call, "event_storage_limit", "Journal full")
            elif outcome == "cancelled":
                await executor._mark_cancelled(run.id, call)
            else:
                await executor._mark_interrupted(run.id, call)

        if code != "event_storage_limit":
            with pytest.raises(ApplicationError) as failure:
                await finish()
            assert failure.value.code == code
            assert repository.state.model_calls[call.id].status is ModelCallStatus.STREAMING
            return
        await finish()
        saved = await repository.get_run(run.id)
        assert saved is not None and saved.status.value == outcome
        assert repository.state.model_calls[call.id].status in {
            ModelCallStatus.FAILED,
            ModelCallStatus.CANCELLED,
        }
        outputs = await repository.list_assistant_outputs(session.id)
        assert outputs[0].content == "Published partial output"
        assert outputs[0].status == "partial"
        assert (await repository.list_run_events(run.id))[-1].event_type.value == f"run.{outcome}"

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("TRELLIS_TEST_DATABASE_URL"),
    reason="Disposable PostgreSQL URLs are required",
)
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "interrupted"])
def test_real_worker_terminal_transition_survives_full_journal(
    tmp_path: Path, outcome: str
) -> None:
    async def scenario():
        async with await psycopg.AsyncConnection.connect(
            os.environ["TRELLIS_TEST_DATABASE_URL"], autocommit=True
        ) as admin:
            owner = str(uuid4())
            await admin.execute("INSERT INTO auth.users(id) VALUES(%s)", (owner,))
            await admin.execute("SET ROLE authenticated")
            await admin.execute("SELECT set_config('request.jwt.claim.sub',%s,false)", (owner,))
            await admin.execute("SELECT trellis.ensure_account()")
            session = await (
                await admin.execute("SELECT trellis.mutate('create_session','{}')")
            ).fetchone()
            assert session is not None
            payload = {
                "session_id": session[0]["id"],
                "client_request_id": str(uuid4()),
                "content": "Hello",
                "model_id": "openai:gpt-5.5",
                "budget_preset": "conservative",
                "submission": {"content": "Hello"},
            }
            accepted = await (
                await admin.execute("SELECT trellis.accept_run(%s)", (Jsonb(payload),))
            ).fetchone()
            assert accepted is not None
            run_id = accepted[0]["id"]
            await admin.execute("RESET ROLE")
            queue = await PostgresWorkerQueue.open(
                os.environ["TRELLIS_WORKER_DATABASE_URL"], max_size=2
            )
            try:
                claim = await queue.claim("journal-proof", str(uuid4()), "journal-capability" * 3)
                assert claim is not None and claim.run_id == run_id
                await queue.begin(claim)
                repository = queue.bind(claim)
                context = await repository.get_execution_context()
                call = await repository.create_model_call(run_id, 1, context.model, {})
                call = await repository.update_model_call(call.id, ModelCallStatus.STREAMING)
                await repository.append_assistant_batch(
                    run_id, call.id, 0, "Published partial output"
                )
                if outcome == "cancelled":
                    await admin.execute("SET ROLE authenticated")
                    await admin.execute(
                        "SELECT trellis.mutate('request_run_cancellation',%s)",
                        (Jsonb({"run_id": run_id}),),
                    )
                    await admin.execute("RESET ROLE")
                await admin.execute(
                    "UPDATE trellis_private.execution_attempts SET operation_storage_bytes=8388608,"
                    "operation_storage_count=32768 WHERE id=%s",
                    (claim.attempt_id,),
                )
                executor = RunExecutor(
                    repository, ServerCredentialStore(make_settings(tmp_path)), {}
                )
                if outcome == "failed":
                    await executor._fail(run_id, call, "event_storage_limit", "Journal full")
                elif outcome == "cancelled":
                    await executor._mark_cancelled(run_id, call)
                else:
                    await executor._mark_interrupted(run_id, call)
                record = await (
                    await admin.execute(
                        "SELECT r.status,j.status,a.status,a.outcome,m.status,o.content "
                        "FROM trellis.runs r JOIN trellis_private.jobs j ON j.run_id=r.id "
                        "JOIN trellis_private.execution_attempts a ON a.id=j.attempt_id "
                        "JOIN trellis.model_calls m ON m.run_id=r.id "
                        "JOIN trellis.run_assistant_outputs o ON o.run_id=r.id WHERE r.id=%s",
                        (run_id,),
                    )
                ).fetchone()
                assert record is not None
                assert record[:4] == (outcome, "finished", "finished", outcome)
                assert record[4] in {"failed", "cancelled"}
                assert record[5] == "Published partial output"
            finally:
                await queue.close()

    asyncio.run(scenario())
