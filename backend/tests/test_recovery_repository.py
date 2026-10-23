import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from run_fixtures import MODEL, model_record, run_record

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.context import CheckpointSource, ContextCheckpointDraft
from app.domain.recovery import RecoveryAction, RecoveryResolution
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


def test_authentication_revision_only_advances_for_the_current_verified_credential() -> None:
    user = UUID("19000000-0000-0000-0000-000000000001")
    source = VerifiedTokenSource(user)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    source.record_verified("current", user, expiry)
    assert source.revision == 1
    source.record_verified("current", user, expiry)
    source.record_verified("older-tab", user, expiry - timedelta(minutes=5))
    assert source.revision == 1 and source.get_token() == "current"
    source.record_verified("refreshed", user, expiry + timedelta(minutes=5))
    assert source.revision == 2 and source.get_token() == "refreshed"


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


def test_recovery_stop_retries_the_same_unknown_outcome_without_retaining_ownership() -> None:
    async def scenario():
        repo, run = repository(), run_record()
        row = asdict(run)
        row.update(
            user_id=str(repo._user_id),
            chat_id=row.pop("session_id"),
            status="interrupted",
            recovery_reason="model_outcome_unknown",
        )
        repo._capabilities[run.id] = "same-capability"
        repo._lease_expires[run.id] = datetime.now(UTC) + timedelta(minutes=1)
        repo._rpc = AsyncMock(
            side_effect=[
                ApplicationError("database_unavailable", "Lost stop acknowledgment"),
                {"run": row, "events": []},
            ]
        )
        try:
            stopped, _ = await repo.stop_recovery(
                run.id,
                RecoveryResolution(
                    RecoveryAction.STOP,
                    "model_outcome_unknown",
                    model_call_id="dispatched-call",
                    usage_unknown=True,
                ),
            )
            assert stopped.recovery_reason == "model_outcome_unknown"
            assert not repo.owns_run_lease(run.id)
            assert repo._rpc.await_args_list[0] == repo._rpc.await_args_list[1]
        finally:
            await repo._client.aclose()

    asyncio.run(scenario())


def test_model_allocation_retries_the_same_saved_request_after_lost_acknowledgment() -> None:
    async def scenario():
        repo, call = repository(), model_record()
        row = asdict(call)
        row.update(user_id=str(repo._user_id))
        row["cached_read_tokens"] = row.pop("cached_tokens")
        repo._capabilities[call.run_id] = "same-capability"
        repo._lease_expires[call.run_id] = datetime.now(UTC) + timedelta(minutes=1)
        repo._rpc = AsyncMock(
            side_effect=[ApplicationError("database_unavailable", "Lost allocation reply"), row]
        )
        try:
            saved = await repo.create_model_call(
                call.run_id, call.step_index, MODEL, call.request_snapshot
            )
            assert saved.id == call.id
            assert repo._rpc.await_args_list[0] == repo._rpc.await_args_list[1]
        finally:
            await repo._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_contract", [False, True])
def test_new_run_persists_the_exact_immutable_contract(missing_contract) -> None:
    async def scenario():
        from test_recovery_contracts import CONTRACT

        repo, run = repository(), run_record()
        contract = replace(CONTRACT, workspace=None)
        row = asdict(run)
        row.update(
            user_id=str(repo._user_id),
            chat_id=row.pop("session_id"),
            lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            recovery_contract=None if missing_contract else contract.to_dict(),
        )
        repo._token_source.record_verified(
            "verified", repo._user_id, datetime.now(UTC) + timedelta(hours=1)
        )
        repo._rpc = AsyncMock(return_value={"run": row, "lease_acquired": True})
        try:
            operation = repo.create_run(
                run.session_id,
                run.turn_id,
                run.client_request_id,
                "hello",
                MODEL,
                recovery_contract=contract,
            )
            if missing_contract:
                with pytest.raises(RuntimeError, match="recovery contract"):
                    await operation
                assert not repo.owns_run_lease(run.id)
            else:
                accepted = await operation
                assert accepted.recovery_contract == contract
            assert repo._rpc.await_args is not None
            assert repo._rpc.await_args.args[1]["recovery_contract"] == contract.to_dict()
        finally:
            await repo._client.aclose()

    asyncio.run(scenario())


def test_user_stop_retries_the_same_authenticated_intent_without_execution_capability() -> None:
    async def scenario():
        repo, run = repository(), run_record()
        row = asdict(run)
        row.update(user_id=str(repo._user_id), chat_id=row.pop("session_id"), status="cancelling")
        repo._rpc = AsyncMock(
            side_effect=[
                ApplicationError("database_unavailable", "Lost cancellation acknowledgment"),
                {"run": row, "event": None},
            ]
        )
        try:
            requested, event = await repo.request_recovery_cancellation(run.id)
            assert requested.status.value == "cancelling" and event is None
            assert not repo.owns_run_lease(run.id)
            assert repo._rpc.await_args_list[0] == repo._rpc.await_args_list[1]
            assert repo._rpc.await_args is not None
            assert repo._rpc.await_args.kwargs["capability"] is None
        finally:
            await repo._client.aclose()

    asyncio.run(scenario())


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


@pytest.mark.parametrize(
    "invalid", [None, "cancelling", "confirmation", "generation", "status", "expiry"]
)
def test_only_a_confirmed_current_takeover_installs_a_local_owner(invalid) -> None:
    async def scenario():
        repo, run = repository(), run_record()
        row = asdict(run)
        row.update(
            user_id=str(repo._user_id),
            chat_id=row.pop("session_id"),
            status=(
                "running"
                if invalid == "status"
                else "cancelling"
                if invalid == "cancelling"
                else "recovering"
            ),
            generation=0 if invalid == "generation" else 1,
            lease_expires_at=(
                datetime.now(UTC) + timedelta(minutes=-1 if invalid == "expiry" else 1)
            ).isoformat(),
        )
        repo._rpc = AsyncMock(
            return_value={"run": row, "lease_acquired": invalid != "confirmation"}
        )
        try:
            if invalid not in {None, "cancelling"}:
                with pytest.raises((RuntimeError, ApplicationError)):
                    await repo.claim_recovery(run, "installation")
                assert not repo.owns_run_lease(run.id)
            else:
                claimed, reason = await repo.claim_recovery(run, "installation")
                assert claimed.generation == 1 and reason is None
                assert repo.owns_run_lease(run.id)
        finally:
            await repo._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong_result", [False, True])
def test_tool_result_acknowledgment_includes_matching_persisted_evidence(wrong_result) -> None:
    async def exercise() -> None:
        repo, run = repository(), run_record()
        metadata: dict[str, object] = {"source": "executor", "workspace_observations": []}
        tool = {
            "id": "tool",
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "assistant_message_id": "assistant",
            "call_index": 0,
            "provider_call_id": "call",
            "name": "run_command",
            "arguments": {},
            "status": "completed",
            "created_at": "now",
            "result_snapshot": None if wrong_result else metadata,
        }
        message = {
            "id": "result",
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "ordinal": 2,
            "role": "tool",
            "content": "Done",
            "tool_call_id": "tool",
            "continuation": [],
            "created_at": "now",
        }
        event = {
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "sequence": 3,
            "event_type": "tool.result",
            "event_version": 1,
            "data": {},
            "created_at": "now",
        }
        repo._capabilities[run.id] = "same-capability"
        repo._lease_expires[run.id] = datetime.now(UTC) + timedelta(minutes=1)
        repo._rpc = AsyncMock(return_value={"tool_call": tool, "message": message, "event": event})
        try:
            operation = repo.record_tool_result(run.id, "tool", "Done", result_snapshot=metadata)
            if wrong_result:
                with pytest.raises(RuntimeError, match="another tool's result"):
                    await operation
            else:
                _, saved, _ = await operation
                assert saved.result_snapshot == metadata
        finally:
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


@pytest.mark.parametrize("changed_checkpoint", [False, True])
def test_summary_receipt_keeps_the_original_checkpoint_and_ownership(changed_checkpoint) -> None:
    async def exercise() -> None:
        repo = repository()
        call, run = model_record(), run_record()
        draft = ContextCheckpointDraft(
            "19000000-0000-0000-0000-000000000004",
            call.id,
            (CheckpointSource("19000000-0000-0000-0000-000000000005", "message", "previous"),),
            "Saved summary",
            "a" * 64,
            "a" * 64,
            "a" * 64,
            "a" * 64,
            100,
            50,
        )
        checkpoint = {
            **asdict(draft),
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "chat_id": run.session_id,
            "revision": 1,
            "parent_id": None,
            "provider_id": run.provider_id,
            "model_id": run.model_id,
            "adapter_kind": run.adapter_kind,
            "covered_message_ordinal": 2,
            "covered_run_message_ordinal": 0,
            "created_at": "now",
        }
        if changed_checkpoint:
            checkpoint["summary"] = "Different summary"
        saved_call = asdict(call)
        saved_call.update(user_id=str(repo._user_id), status="completed")
        saved_call["cached_read_tokens"] = saved_call.pop("cached_tokens")
        requests: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(200, content=b"Lost JSON acknowledgment")
            return httpx.Response(
                200, json={"model_call": saved_call, "checkpoint": checkpoint, "events": []}
            )

        repo._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        repo._token_source.record_verified(
            "verified", repo._user_id, datetime.now(UTC) + timedelta(hours=1)
        )
        repo._capabilities[call.run_id] = "same-capability"
        repo._lease_expires[call.run_id] = datetime.now(UTC) + timedelta(minutes=1)
        try:
            if changed_checkpoint:
                with pytest.raises(RuntimeError, match="another summary"):
                    await repo.commit_summary(run.id, call.id, {}, 0, draft)
            else:
                _, saved, _ = await repo.commit_summary(run.id, call.id, {}, 0, draft)
                assert saved.draft == draft
            assert requests[0] == requests[1]
            assert repo.owns_run_lease(run.id)
        finally:
            await repo._client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("different_attempt", [False, True])
def test_lost_tool_dispatch_ack_reuses_the_attempt_identity(different_attempt) -> None:
    async def exercise() -> None:
        repo = repository()
        run = run_record()
        dispatch_id = "19000000-0000-0000-0000-000000000010"
        tool = {
            "id": "19000000-0000-0000-0000-000000000011",
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "assistant_message_id": "assistant",
            "call_index": 0,
            "provider_call_id": "provider-call",
            "name": "read_file",
            "arguments": {},
            "status": "running",
            "created_at": "now",
            "dispatch_attempt": 1,
            "dispatch_generation": 0,
            "dispatched_at": "now",
            "recovery_policy": "read_only",
            "dispatch_id": "other-attempt" if different_attempt else dispatch_id,
            "input_snapshot": {"arguments": {}, "approval_preview": None},
        }
        event = {
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "sequence": 3,
            "event_type": "run.phase",
            "event_version": 1,
            "data": {"phase": "tool_use"},
            "created_at": "now",
        }
        requests: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                raise httpx.ReadError("Dispatch committed, acknowledgment lost", request=request)
            return httpx.Response(200, json={"tool_call": tool, "event": event})

        repo._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        repo._token_source.record_verified(
            "verified", repo._user_id, datetime.now(UTC) + timedelta(hours=1)
        )
        repo._capabilities[run.id] = "same-capability"
        repo._lease_expires[run.id] = datetime.now(UTC) + timedelta(minutes=1)
        try:
            pending = repo.begin_tool_execution(
                run.id,
                str(tool["id"]),
                dispatch_id=dispatch_id,
                expected_attempt=0,
                recovery_policy="read_only",
                input_snapshot={"arguments": {}, "approval_preview": None},
            )
            if different_attempt:
                with pytest.raises(RuntimeError, match="another tool"):
                    await pending
            else:
                saved, _ = await pending
                assert saved.dispatch_id == dispatch_id
            assert requests[0] == requests[1]
        finally:
            await repo._client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("different_attempt", [False, True])
def test_retry_authorization_preserves_reconciliation_proof_and_attempt(different_attempt) -> None:
    async def exercise() -> None:
        repo, run = repository(), run_record()
        tool = {
            "id": "tool",
            "user_id": str(repo._user_id),
            "run_id": run.id,
            "assistant_message_id": "assistant",
            "call_index": 0,
            "provider_call_id": "call",
            "name": "apply_patch",
            "arguments": {},
            "status": "pending",
            "created_at": "now",
            "dispatch_attempt": 2 if different_attempt else 1,
            "dispatch_id": "dispatch",
            "result_snapshot": {"workspace_observations": []},
        }
        proof: dict[str, object] = {
            "before_sha256": "a" * 64,
            "after_sha256": "b" * 64,
            "observed_sha256": "a" * 64,
        }
        repo._capabilities[run.id] = "same-capability"
        repo._lease_expires[run.id] = datetime.now(UTC) + timedelta(minutes=1)
        repo._rpc = AsyncMock(
            side_effect=[ApplicationError("database_unavailable", "Lost acknowledgment"), tool]
        )
        try:
            pending = repo.authorize_tool_retry(
                run.id,
                "tool",
                expected_dispatch_id="dispatch",
                expected_attempt=1,
                reconciliation="unchanged_original",
                proof=proof,
            )
            if different_attempt:
                with pytest.raises(RuntimeError, match="another tool retry"):
                    await pending
            else:
                saved = await pending
                assert saved.result_snapshot == {"workspace_observations": []}
            assert repo._rpc.await_args_list[0] == repo._rpc.await_args_list[1]
            assert repo._rpc.await_args is not None
            assert repo._rpc.await_args.args[1]["proof"] == proof
        finally:
            await repo._client.aclose()

    asyncio.run(exercise())
