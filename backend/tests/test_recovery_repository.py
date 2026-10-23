import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from run_fixtures import model_record, run_record

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource


def repository() -> SupabaseRepository:
    user = UUID("19000000-0000-0000-0000-000000000001")
    return SupabaseRepository(
        Settings(
            _env_file=None,
            supabase_url="https://test.supabase.co",
            supabase_publishable_key="sb_publishable_test",
        ),
        user,
        VerifiedTokenSource(user),
        httpx.AsyncClient(),
    )


def test_recovery_metadata_is_not_dropped_when_reading_a_run() -> None:
    repo = repository()
    row = asdict(run_record())
    row["chat_id"] = row.pop("session_id")
    row["user_id"] = str(repo._user_id)
    row.update(
        generation=2,
        retry_allowed=False,
        recovery_reason="command_outcome_unknown",
        uncertain_tool_call_id="uncertain-operation",
        recovery_contract={
            "version": 1,
            "runtime_version": "checkpoint-runtime-v1",
            "adapter_versions": {"responses": "v1"},
            "tool_versions": {},
            "request_identity": "request",
            "installation_id": "19000000-0000-0000-0000-000000000002",
            "workspace": None,
        },
    )
    run = repo._run(row)
    assert run.generation == 2
    assert not run.retry_allowed
    assert run.recovery_reason == "command_outcome_unknown"
    assert run.uncertain_tool_call_id == "uncertain-operation"
    assert run.recovery_contract is not None
    assert run.recovery_contract.to_dict() == row["recovery_contract"]
    row["recovery_contract"] = {"version": 999}
    with pytest.raises(ValueError):
        repo._run(row)


def test_legacy_run_has_no_implicit_recovery_contract() -> None:
    repo = repository()
    row = asdict(run_record())
    row["chat_id"] = row.pop("session_id")
    row["user_id"] = str(repo._user_id)
    row.pop("recovery_contract", None)
    assert repo._run(row).recovery_contract is None


def test_lost_claim_acknowledgment_retries_the_identical_ownership_capability() -> None:
    async def exercise() -> None:
        repo = repository()
        saved = {"generation": 1}
        repo._rpc = AsyncMock(
            side_effect=[ApplicationError("database_unavailable", "Acknowledgment lost"), saved]
        )
        payload: dict[str, object] = {"run_id": "run", "expected_generation": 0}
        assert (
            await repo._idempotent_rpc("claim_recovery", payload, capability="same-capability")
            == saved
        )
        assert repo._rpc.await_count == 2
        assert repo._rpc.await_args_list[0] == repo._rpc.await_args_list[1]
        repo._rpc = AsyncMock(side_effect=ApplicationError("cloud_access_denied", "Expired"))
        with pytest.raises(ApplicationError):
            await repo._idempotent_rpc("claim_recovery", payload, capability="same-capability")
        assert repo._rpc.await_count == 1
        await repo._client.aclose()

    asyncio.run(exercise())


def test_generation_commit_retries_lost_ack_without_republishing_or_changing_identity() -> None:
    async def exercise() -> None:
        repo = repository()
        call = model_record()
        row = asdict(call)
        row["user_id"] = str(repo._user_id)
        row["cached_read_tokens"] = row.pop("cached_tokens")
        row["status"] = "completed"
        payloads: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                raise httpx.ReadError("Saved commit, lost acknowledgment", request=request)
            return httpx.Response(200, json={"model_call": row, "tool_calls": [], "events": []})

        repo._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        repo._token_source.record_verified(
            "verified", repo._user_id, datetime.now(UTC) + timedelta(hours=1)
        )
        repo._capabilities[call.run_id] = "same-capability"
        repo._lease_expires[call.run_id] = datetime.now(UTC) + timedelta(minutes=1)
        saved, _, _ = await repo.commit_generation(
            call.run_id,
            call.id,
            {"response_snapshot": {"content": "answer", "tool_calls": []}, "cached_tokens": 0},
            assistant_message=None,
            final_content="answer",
        )
        assert saved.id == call.id
        assert payloads[0] == payloads[1]
        assert payloads[0]["p_action"] == "commit_generation"
        assert not repo.owns_run_lease(call.run_id)
        await repo._client.aclose()

    asyncio.run(exercise())
