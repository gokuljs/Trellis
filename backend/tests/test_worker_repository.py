import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.application.errors import ApplicationError
from app.domain.jobs import ExecutionClaim
from app.domain.runtime import RunStatus
from app.infrastructure import worker_repository as module
from app.infrastructure.worker_repository import PostgresWorkerQueue, _ScopedTransport
from tests.test_supabase_repository import _run_row


def claim() -> ExecutionClaim:
    return ExecutionClaim("job", str(_run_row()["id"]), "worker", "attempt", "x" * 40, "expiry")


class FakePool:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    async def open(self, **kwargs: object) -> None:
        return None

    async def close(self, **kwargs: object) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self, **kwargs: object):
        yield self

    async def execute(self, statement: str, parameters: object = None):
        self.calls.append((statement, parameters))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.response = response
        return self

    async def fetchone(self):
        return self.response


def queue(pool: FakePool) -> PostgresWorkerQueue:
    return PostgresWorkerQueue(cast(AsyncConnectionPool, pool))


def test_restricted_login_is_required_and_connections_close_on_rejection(monkeypatch):
    async def exercise():
        for identity in [("admin", "trellis_runtime_worker"), ("trellis_runtime_worker", "admin")]:
            pool = FakePool([identity])
            monkeypatch.setattr(module, "AsyncConnectionPool", lambda *a, chosen=pool, **kw: chosen)
            with pytest.raises(ValueError, match="restricted worker login"):
                await PostgresWorkerQueue.open("opaque-connection-string")
            assert pool.closed
        pool = FakePool([("trellis_runtime_worker", "trellis_runtime_worker")])
        monkeypatch.setattr(module, "AsyncConnectionPool", lambda *a, chosen=pool, **kw: chosen)
        opened = await PostgresWorkerQueue.open("opaque-connection-string")
        await opened.close()
        assert pool.closed
        for limit in [True, 0, 17]:
            with pytest.raises(ValueError, match="capacity"):
                await PostgresWorkerQueue.open("unused", max_size=limit)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.InsufficientPrivilege("capability detail"), "run_not_owned"),
        (
            psycopg.errors.InvalidParameterValue("checkpoint revision conflict"),
            "context_checkpoint_conflict",
        ),
        (psycopg.errors.InvalidParameterValue("private detail"), "worker_operation_invalid"),
        (psycopg.errors.CheckViolation("private detail"), "worker_operation_invalid"),
        (psycopg.OperationalError("password detail"), "database_unavailable"),
        (TimeoutError("private detail"), "database_unavailable"),
    ],
)
def test_database_failures_are_bounded_and_sanitized(error: Exception, code: str):
    async def exercise():
        pool = FakePool([error, error])
        with pytest.raises(ApplicationError) as caught:
            await queue(pool)._call("SELECT 1", ())
        assert caught.value.code == code
        assert "detail" not in caught.value.message
        assert len(pool.calls) <= 2

    asyncio.run(exercise())


def test_retry_preserves_publication_operation_identity():
    async def exercise():
        pool = FakePool([psycopg.OperationalError("lost response"), ({"sequence": 3},)])
        transport = _ScopedTransport(queue(pool), claim())
        result = await transport._rpc("append_run_event", {"data": {"text": "hello"}})
        assert result == {"sequence": 3}
        assert pool.calls[0] == pool.calls[1]
        assert pool.calls[0][0] == "SELECT trellis_private.worker_mutate(%s,%s,%s,%s)"
        with pytest.raises(PermissionError):
            await transport._rpc("get_profile", {})

    asyncio.run(exercise())


def test_queue_methods_return_contract_records_and_reject_invalid_counts():
    async def exercise():
        assignment = claim()
        row = {**_run_row(), "status": "running"}
        pool = FakePool(
            [(asdict(assignment),), (row,), (row,), (None,), (3,), (True,), (None,), None]
        )
        worker = queue(pool)
        assert await worker.claim("worker", "attempt", assignment.capability) == assignment
        assert (await worker.begin(assignment)).status == RunStatus.RUNNING
        assert (await worker.renew(assignment)).id == assignment.run_id
        await worker.release_unstarted(assignment)
        assert await worker.recover_expired(3) == 3
        with pytest.raises(RuntimeError, match="invalid count"):
            await worker.recover_expired()
        assert await worker.claim("worker", "attempt", assignment.capability) is None
        with pytest.raises(RuntimeError, match="no result"):
            await worker._call("SELECT 1", ())
        for invalid in [True, 0, 101]:
            with pytest.raises(ValueError, match="recovery limit"):
                await worker.recover_expired(invalid)
        assert worker.bind(assignment).claim == assignment

    asyncio.run(exercise())


def test_private_transport_has_no_account_storage_boundary():
    async def exercise():
        transport = _ScopedTransport(queue(FakePool([])), claim())
        for method, path in [("GET", "profiles"), ("POST", "rpc/ensure_account")]:
            with pytest.raises(PermissionError):
                await transport._request(method, path)
        with pytest.raises(PermissionError):
            await transport._request("GET", "messages", params={"id": "in.(foreign)"})
        with pytest.raises(ApplicationError, match="outside"):
            transport._run_capability(str(uuid4()))

    asyncio.run(exercise())


def test_database_operation_budget_includes_retry_backoff(monkeypatch):
    monkeypatch.setattr(module, "_DATABASE_OPERATION_TIMEOUT", 0.01)

    async def exercise():
        pool = FakePool([psycopg.OperationalError("lost response"), ({"sequence": 3},)])
        transport = _ScopedTransport(queue(pool), claim())
        with pytest.raises(ApplicationError) as caught:
            await transport._rpc("append_run_event", {"data": {"text": "hello"}})
        assert caught.value.code == "database_unavailable"
        assert len(pool.calls) == 1

    asyncio.run(exercise())
