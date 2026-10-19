from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from run_fixtures import Provider, Repository, service

from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.domain.runtime import RunSnapshot


class AcceptedRepository(Repository):
    def __init__(self) -> None:
        super().__init__()
        self.fingerprint: str | None = None
        self.created = 0
        self.accepting = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def match_run_request(self, session_id, request_id, **submission):
        if self.fingerprint is None:
            return None
        if self.fingerprint != submission["request_fingerprint"]:
            raise ValueError("run request conflicts with a previous payload")
        return self.run

    async def lookup_run(self, session_id, request_id):
        return self.run if self.fingerprint else None

    async def create_run(self, *args, **kwargs):
        self.accepting.set()
        await self.release.wait()
        self.created += 1
        self.fingerprint = kwargs["request_fingerprint"]
        return self.run


def test_retry_finds_saved_run_before_changed_defaults_or_credentials(monkeypatch) -> None:
    async def exercise() -> None:
        repository = AcceptedRepository()
        runtime = service(repository, Provider())
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        original = await runtime.create_run("session", "request", "hello")
        monkeypatch.setattr(
            runtime._settings,
            "list_models",
            AsyncMock(side_effect=AssertionError("mutable defaults")),
        )
        monkeypatch.setattr(
            runtime._secret_store, "get", AsyncMock(side_effect=AssertionError("credentials"))
        )
        assert await runtime.create_run("session", "request", " hello ") == original
        assert repository.created == 1
        assert await runtime.lookup_run("session", "request") == original
        await runtime.close()

    asyncio.run(exercise())


def test_secret_change_and_explicit_option_change_conflict(monkeypatch) -> None:
    async def exercise() -> None:
        repository = AcceptedRepository()
        runtime = service(repository, Provider())
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        secret_a = "sk-proj-" + "a" * 80
        secret_b = "sk-proj-" + "b" * 80
        await runtime.create_run("session", "request", secret_a)
        submissions: list[tuple[str, BudgetPreset | None]] = [
            (secret_b, None),
            (secret_a, "longer"),
        ]
        for content, budget in submissions:
            with pytest.raises(ApplicationError) as error:
                await runtime.create_run("session", "request", content, budget_preset=budget)
            assert error.value.code == "run_conflict"
        await runtime.close()

    asyncio.run(exercise())


def test_admission_overload_still_matches_saved_requests(monkeypatch) -> None:
    async def exercise() -> None:
        repository = AcceptedRepository()
        runtime = service(repository, Provider())
        monkeypatch.setattr(runtime, "_schedule", lambda run_id: None)
        original = await runtime.create_run("session", "request", "hello")

        async def paused_admission() -> RunSnapshot:
            await asyncio.Event().wait()
            return original

        pending = [asyncio.create_task(paused_admission()) for _ in range(32)]
        runtime._admissions.update(pending)
        try:
            assert await runtime.create_run("session", "request", "hello") == original
            with pytest.raises(ApplicationError) as conflict:
                await runtime.create_run("session", "request", "different")
            assert conflict.value.code == "run_conflict"
            repository.fingerprint = None
            with pytest.raises(ApplicationError) as full:
                await runtime.create_run("session", "new", "hello")
            assert full.value.code == "run_capacity"
            assert repository.created == 1
        finally:
            runtime._admissions.clear()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await runtime.close()

    asyncio.run(exercise())


def test_cancelled_connection_does_not_abandon_acceptance_and_shutdown_joins_it(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        repository = AcceptedRepository()
        repository.release.clear()
        runtime = service(repository, Provider())
        scheduled: list[str] = []
        monkeypatch.setattr(runtime, "_schedule", scheduled.append)
        submitted = asyncio.create_task(runtime.create_run("session", "request", "hello"))
        await repository.accepting.wait()
        submitted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submitted
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
        repository.release.set()
        await closing
        assert repository.created == 1
        assert scheduled == [repository.run.id]
        assert not runtime._admissions

    asyncio.run(exercise())


@pytest.mark.parametrize("committed,owned", [(True, True), (False, True), (True, False)])
@pytest.mark.parametrize("failure", ["disconnect", "invalid_json", "non_object"])
def test_lost_acceptance_response_reconciles_same_capability(committed, owned, failure) -> None:
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    import httpx
    from run_fixtures import MODEL, run_record

    from app.core.config import Settings
    from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

    user = UUID("19000000-0000-0000-0000-000000000001")
    row = asdict(run_record())
    row["chat_id"] = row.pop("session_id")
    row["user_id"] = str(user)
    row["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            if failure == "non_object":
                return httpx.Response(200, json=[])
            if failure == "invalid_json":
                return httpx.Response(200, content=b"lost JSON response")
            raise httpx.ReadError("lost response", request=request)
        if payload["p_action"] == "match_run_request" and not committed:
            return httpx.Response(200, json=None)
        return httpx.Response(200, json={"run": row, "lease_acquired": owned})

    async def exercise() -> None:
        source = VerifiedTokenSource(user)
        source.record_verified("verified", user, datetime.now(UTC) + timedelta(hours=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repo = SupabaseRepository(
                Settings(
                    _env_file=None,
                    supabase_url="https://test.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                user,
                source,
                client,
            )
            if failure == "non_object":
                with pytest.raises(RuntimeError, match="invalid object"):
                    await repo.create_run(str(row["chat_id"]), "turn", "request", "hello", MODEL)
                assert not repo._capabilities
                return
            run = await repo.create_run(
                str(row["chat_id"]),
                "turn",
                "request",
                "hello",
                MODEL,
                request_fingerprint="a" * 64,
            )
            assert run.id == row["id"]
            assert repo.owns_run_lease(run.id) is owned

    asyncio.run(exercise())
    assert [request["p_action"] for request in requests] == (
        ["create_run"]
        if failure == "non_object"
        else ["create_run", "match_run_request"]
        if committed
        else ["create_run", "match_run_request", "create_run"]
    )
    assert len({request["p_capability"] for request in requests}) == 1
    assert requests[0]["p_payload"] == requests[-1]["p_payload"]
