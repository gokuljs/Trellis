import asyncio
import logging
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.domain.runtime import RunStatus
from app.infrastructure.secrets import SecretStore
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.memory_repository import MemoryRepository
from tests.support import account_context, verified_token_source
from tests.test_runtime_api import StreamingProvider, configured_client
from tests.test_supabase_repository import ALICE, SETTINGS, _model, _run_row


def test_acceptance_remains_queued_and_retries_before_mutable_configuration(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "memory")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        provider = StreamingProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            acceptance_repository=database,
        )
        accepted = await service.accept_run(session.id, "request", "  Hello  ")
        assert accepted.status is RunStatus.QUEUED
        assert accepted.deadline_at is None and accepted.lease_expires_at is None
        assert provider.request is None
        assert not database.owns_run_lease(accepted.id)
        assert (await database.list_messages(session.id))[0].content == "Hello"
        database.state.models.clear()
        database.state.selected_model_id = "missing"
        database.state.default_budget_preset = "longer"
        await secrets.delete("openai")
        retried = await service.accept_run(session.id, "request", "Hello")
        assert retried.id == accepted.id and retried.budget_preset == "conservative"
        assert await service.lookup_run(session.id, "request") == accepted
        assert len(database.state.runs) == 1
        assert len(database.state.events[accepted.id]) == 1
        await service.close()
        persisted = await database.get_run(accepted.id)
        assert persisted is not None and persisted.status is RunStatus.QUEUED

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["content", "turn", "model", "budget"])
def test_retry_rejects_changed_original_submission(tmp_path: Path, change: str) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "memory")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": StreamingProvider()},
            acceptance_repository=database,
        )
        content = "sk-originalsecret123"
        await service.accept_run(session.id, "request", content)
        assert (await database.list_messages(session.id))[0].content == "[REDACTED]"
        with pytest.raises(ApplicationError) as failure:
            await service.accept_run(
                session.id,
                "request",
                "sk-anothersecret123" if change == "content" else content,
                turn_id="other" if change == "turn" else None,
                model_id="openai:gpt-5.5" if change == "model" else None,
                budget_preset="conservative" if change == "budget" else None,
            )
        assert failure.value.code == "run_conflict"
        assert len(database.state.runs) == 1
        await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "reason, code",
    [
        ("foreign", "session_not_found"),
        ("missing_provider", "provider_not_configured"),
        ("missing_model", "model_not_available"),
        ("empty", "message_empty"),
        ("long", "run_invalid"),
    ],
)
def test_acceptance_rejects_invalid_new_work(tmp_path: Path, reason: str, code: str) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "memory")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        if reason != "missing_provider":
            await secrets.set("openai", "sk-test")
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": StreamingProvider()},
            acceptance_repository=database,
        )
        content = " " if reason == "empty" else "Hello"
        if reason == "long":
            content = "x" * 100001
        with pytest.raises(ApplicationError) as failure:
            await service.accept_run(
                "foreign" if reason == "foreign" else session.id,
                "request",
                content,
                model_id="missing" if reason == "missing_model" else None,
            )
        assert failure.value.code == code
        assert not database.state.runs

    asyncio.run(exercise())


def test_lookup_websocket_restores_queued_run_and_history_deadline(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.api.runtime_logging")
    provider = StreamingProvider()
    with configured_client(tmp_path, provider) as client:
        session_id = client.post("/api/sessions").json()["id"]
        service = account_context(client.app).run_service
        assert client.portal is not None
        accepted = client.portal.call(partial(service.accept_run, session_id, "request", "Hello"))
        with client.websocket_connect("/api/runtime") as websocket:
            for request, session, expected in [
                ("request", session_id, accepted.id),
                ("missing", session_id, None),
                ("request", str(uuid4()), "session_not_found"),
            ]:
                websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": "lookup",
                        "method": "run.lookup",
                        "params": {"sessionId": session, "clientRequestId": request},
                    }
                )
                response = websocket.receive_json()
                if expected == "session_not_found":
                    assert response["error"]["data"]["code"] == expected
                elif expected is None:
                    assert response["result"] is None
                else:
                    assert response["result"]["runId"] == expected
                    assert response["result"]["status"] == "queued"
                    assert response["result"]["limits"]["deadlineAt"] is None
        summaries = client.get(f"/api/sessions/{session_id}/runs").json()
        assert summaries["items"][0]["deadline_at"] is None
        assert provider.request is None
    commands = [
        vars(record)
        for record in caplog.records
        if record.name == "app.api.runtime_logging" and vars(record).get("command") == "run.lookup"
    ]
    assert len(commands) == 3
    assert commands[0]["run_id"] == accepted.id
    assert commands[0]["outcome"] == commands[1]["outcome"] == "completed"
    assert commands[2]["outcome"] == "rejected"
    assert "run_id" not in commands[1]
    assert "run_id" not in commands[2]


def test_durable_repository_uses_user_authority_without_execution_token_lifetime() -> None:
    async def exercise() -> None:
        import json
        from datetime import UTC, datetime, timedelta

        source = verified_token_source(ALICE)
        source._expires_at = datetime.now(UTC) + timedelta(seconds=45)
        requests = []
        row = {**_run_row(), "deadline_at": None, "lease_expires_at": None}

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("lookup_run"):
                return httpx.Response(200, json=None)
            return httpx.Response(200, json=row)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, source, client)
            submission: dict[str, object] = {"content": "Original", "turnId": "turn-one"}
            assert await repository.lookup_run("chat", "request") is None
            restored = await repository.match_run_request("chat", "request", submission)
            accepted = await repository.accept_run(
                "chat", "request", "Redacted", _model(), submission
            )
            assert restored is not None and restored.id == accepted.id
            assert accepted.deadline_at is None
            assert not repository.owns_run_lease(accepted.id)
            assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
                "lookup_run",
                "match_run_request",
                "accept_run",
            ]
            assert all(
                request.headers["Authorization"] == "Bearer alice-token" for request in requests
            )
            payload = json.loads(requests[-1].content)["p_payload"]
            assert payload["submission"]["content"] == "Original"
            assert payload["content"] == "Redacted"
            assert "p_capability" not in json.loads(requests[-1].content)

    asyncio.run(exercise())
