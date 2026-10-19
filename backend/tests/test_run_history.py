import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_run_service, log_owned_chat
from app.api.routes.sessions import router, serialize_run_summary
from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.core.config import Settings
from app.domain.runtime import RunSnapshot, RunStatus, RunSummary
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

USER_ID = UUID("16000000-0000-0000-0000-000000000001")


def run_record() -> RunSnapshot:
    return RunSnapshot(
        id="run-id",
        session_id="session-id",
        turn_id="turn-id",
        status=RunStatus.COMPLETED,
        provider_id="openai",
        model_id="openai:gpt-5.5",
        adapter_kind="openai",
        upstream_model_id="gpt-5.5",
        input_message_id="input-id",
        retry_of=None,
        client_request_id="request-id",
        max_model_calls=8,
        max_tool_calls=16,
        budget_preset="conservative",
        max_total_tokens=100_000,
        max_cost_usd=2.0,
        deadline_at="2026-10-16T00:10:00+00:00",
        cancel_requested_at=None,
        lease_expires_at=None,
        recovery_count=0,
        stop_reason="final_response",
        last_event_sequence=900,
        error_code=None,
        error_message=None,
        created_at="2026-10-16T00:00:00+00:00",
        started_at="2026-10-16T00:00:01+00:00",
        finished_at="2026-10-16T00:00:10+00:00",
    )


def summary_record() -> RunSummary:
    return RunSummary(run_record(), 3, 2, 1234, False, 0.012, False)


def summary_row() -> dict:
    row = asdict(summary_record())
    row["run"]["user_id"] = str(USER_ID)
    row["run"]["chat_id"] = row["run"].pop("session_id")
    return row


def repository(client: httpx.AsyncClient) -> SupabaseRepository:
    source = VerifiedTokenSource(USER_ID)
    source.record_verified("user-token", USER_ID, datetime.now(UTC) + timedelta(hours=1))
    return SupabaseRepository(
        Settings(
            _env_file=None,
            supabase_url="https://test.supabase.co",
            supabase_publishable_key="sb_publishable_test",
        ),
        USER_ID,
        source,
        client,
    )


def test_summary_repository_uses_one_bounded_rpc_without_event_reads() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[summary_row()])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await repository(client).list_run_summaries_for_session(
                "session-id", 100, 101, turn_id="turn-id", newest_first=True
            )
            assert result == [summary_record()]

    asyncio.run(exercise())
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/read_session_run_summaries"
    assert requests[0].headers["Authorization"] == "Bearer user-token"
    assert json.loads(requests[0].content) == {
        "p_chat_id": "session-id",
        "p_offset": 100,
        "p_limit": 101,
        "p_turn_id": "turn-id",
        "p_newest_first": True,
    }


@pytest.mark.parametrize("field", ["user_id", "chat_id", "turn_id"])
def test_summary_repository_rejects_mismatched_ownership_and_scope(field) -> None:
    row = summary_row()
    row["run"][field] = "another-owner-or-chat"

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[row]))
        ) as client:
            with pytest.raises(RuntimeError):
                await repository(client).list_run_summaries_for_session(
                    "session-id", 0, 10, turn_id="turn-id"
                )

    asyncio.run(exercise())


def test_summary_http_api_preserves_partial_usage_without_event_history() -> None:
    app = FastAPI()
    app.include_router(router)
    service = AsyncMock()
    service.list_session_runs.return_value = [summary_record()], 100
    app.dependency_overrides[get_run_service] = lambda: service
    app.dependency_overrides[log_owned_chat] = lambda: None

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/sessions/session-id/runs?limit=100")
            assert response.status_code == 200
            body = response.json()
            assert body["next_offset"] == 100
            item = body["items"][0]
            assert item["run_id"] == "run-id"
            assert item["model_call_count"] == 3 and item["tool_call_count"] == 2
            assert item["total_tokens"] == 1234 and item["usage_complete"] is False
            assert item["estimated_cost_usd"] == 0.012 and item["cost_complete"] is False
            assert item["max_cost_usd"] == 2.0 and item["max_total_tokens"] == 100_000
            assert item["provider_id"] == "openai" and item["model_id"] == "openai:gpt-5.5"
            assert item["stop_reason"] == "final_response" and item["error_message"] is None

    asyncio.run(exercise())


def test_summary_explains_recovered_run_without_diagnostic_trace() -> None:
    summary = replace(
        summary_record(),
        run=replace(run_record(), status=RunStatus.INTERRUPTED, stop_reason="lease_expired"),
    )
    response = serialize_run_summary(summary)
    assert response.error_code == "lease_expired"
    assert response.error_message == (
        "The API stopped before this run finished. Start a new run to continue."
    )


def test_summary_service_requires_owned_session_before_reading_runs() -> None:
    async def exercise() -> None:
        repo = AsyncMock()
        repo.get_session.return_value = None
        service = RunService(repo, repo, repo, repo, repo, {})
        with pytest.raises(ApplicationError, match="Session not found"):
            await service.list_session_runs("missing-session", 0, 10)
        repo.list_run_summaries_for_session.assert_not_awaited()

    asyncio.run(exercise())


def test_summary_service_refreshes_aggregates_once_after_lease_recovery() -> None:
    async def exercise() -> None:
        repo = AsyncMock()
        repo.get_session.return_value = object()
        active = replace(
            summary_record(),
            run=replace(
                run_record(),
                status=RunStatus.RUNNING,
                lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            ),
        )
        recovered = replace(
            active,
            run=replace(active.run, status=RunStatus.INTERRUPTED, stop_reason="lease_expired"),
        )
        repo.list_run_summaries_for_session.side_effect = [[active], [recovered]]
        lease = AsyncMock()
        lease.recover_expired_run.return_value = recovered.run
        service = RunService(repo, repo, repo, repo, repo, {}, lease_repository=lease)
        summaries, next_offset = await service.list_session_runs("session-id", 0, 10)
        assert summaries == [recovered] and next_offset is None
        assert repo.list_run_summaries_for_session.await_count == 2
        lease.recover_expired_run.assert_awaited_once_with("run-id")
        repo.get_run.assert_not_awaited()
        repo.list_model_calls.assert_not_awaited()

    asyncio.run(exercise())
