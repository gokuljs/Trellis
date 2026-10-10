"""Defense-in-depth checks at the cloud repository boundary."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.runtime import RunStatus, ToolApprovalDecision, ToolCallStatus
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

ALICE = UUID("c928705a-6f03-4aa8-9f81-c4fb50696527")
BOB = UUID("55114a4d-aa42-4eb6-a61d-f91c7992c06d")
SETTINGS = Settings(
    environment="development",
    supabase_url="http://localhost:54321",
    supabase_publishable_key="sb_publishable_test-key",
)


def _source() -> VerifiedTokenSource:
    source = VerifiedTokenSource(ALICE)
    source.record_verified("alice-token", ALICE, datetime.now(UTC) + timedelta(hours=1))
    return source


@pytest.mark.parametrize("token", ["", "has whitespace", "nonascii-\u00e9", "x" * 16_385])
def test_verified_token_source_rejects_unsafe_bearer_token_format(token: str) -> None:
    source = VerifiedTokenSource(ALICE)
    with pytest.raises(ValueError, match="invalid format"):
        source.record_verified(token, ALICE, datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(RuntimeError, match="verified token is required"):
        source.get_token()


def test_verified_token_source_rejects_naive_expiry() -> None:
    source = VerifiedTokenSource(ALICE)
    with pytest.raises(ValueError, match="timezone aware"):
        source.record_verified("alice-token", ALICE, datetime.now() + timedelta(hours=1))


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(401, "cloud_access_denied"), (403, "cloud_access_denied"), (503, "database_unavailable")],
)
def test_cloud_errors_do_not_expose_server_details(status_code: int, expected_code: str) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"code": "private_internal_code", "message": "private server detail"},
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ApplicationError) as error:
                await repository.get_profile()
            assert error.value.code == expected_code
            assert "private" not in error.value.message

    asyncio.run(exercise())


def test_network_timeout_is_a_sanitized_cloud_failure() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ApplicationError) as error:
                await repository.get_profile()
            assert error.value.code == "database_unavailable"
            assert "private" not in error.value.message

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("server_message", "expected_message"),
    [
        ("Invalid requested run state", "Invalid requested run state"),
        ("private detail " * 30, "The cloud operation is invalid"),
    ],
)
def test_cloud_validation_error_only_returns_bounded_client_safe_text(
    server_message: str, expected_message: str
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "22023", "message": server_message})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ValueError, match=expected_message):
                await repository.set_default_budget_preset("conservative")

    asyncio.run(exercise())


def test_cloud_success_response_with_invalid_json_is_rejected() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(RuntimeError, match="invalid JSON"):
                await repository.get_profile()

    asyncio.run(exercise())


def test_repository_rejects_a_cross_account_profile_even_if_rls_is_bypassed() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(BOB),
                    "display_name": "Bob",
                    "created_at": "2026-10-10T10:00:00+00:00",
                    "updated_at": "2026-10-10T10:00:00+00:00",
                }
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(RuntimeError, match="another account"):
                await repository.get_profile()

    asyncio.run(exercise())


def test_repository_rejects_invalid_message_count() -> None:
    chat_id = "5f2b6daa-65db-43ad-b0ad-742e5c3645b9"

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": chat_id,
                    "user_id": str(ALICE),
                    "title": "A chat",
                    "workspace_path": None,
                    "created_at": "2026-10-10T10:00:00+00:00",
                    "updated_at": "2026-10-10T10:00:00+00:00",
                    "message_count": "unknown",
                }
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(RuntimeError, match="invalid message count"):
                await repository.get_session(chat_id)

    asyncio.run(exercise())


def test_repository_rejects_another_accounts_chat_summary() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "5f2b6daa-65db-43ad-b0ad-742e5c3645b9",
                    "user_id": str(BOB),
                    "title": "Bob's chat",
                    "workspace_path": None,
                    "created_at": "2026-10-10T10:00:00+00:00",
                    "updated_at": "2026-10-10T10:00:00+00:00",
                    "message_count": 1,
                }
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(RuntimeError, match="another account"):
                await repository.list_sessions()

    asyncio.run(exercise())


def test_decided_approvals_are_discovered_only_for_live_locally_owned_runs() -> None:
    now = datetime.now(UTC)
    live = ("approved", "undecided", "completed_tool", "running", "missing")
    queried_runs: list[str] = []
    queried_tools: list[str] = []
    runs = {
        "approved": SimpleNamespace(status=RunStatus.WAITING_FOR_APPROVAL),
        "undecided": SimpleNamespace(status=RunStatus.WAITING_FOR_APPROVAL),
        "completed_tool": SimpleNamespace(status=RunStatus.WAITING_FOR_APPROVAL),
        "running": SimpleNamespace(status=RunStatus.RUNNING),
        "missing": None,
    }

    async def get_run(run_id: str) -> SimpleNamespace | None:
        queried_runs.append(run_id)
        return runs[run_id]

    async def list_tool_calls(run_id: str) -> list[SimpleNamespace]:
        queried_tools.append(run_id)
        decisions = {
            "approved": (ToolCallStatus.PENDING, ToolApprovalDecision.APPROVED),
            "undecided": (ToolCallStatus.PENDING, None),
            "completed_tool": (ToolCallStatus.COMPLETED, ToolApprovalDecision.APPROVED),
        }
        status, decision = decisions[run_id]
        return [SimpleNamespace(status=status, approval_decision=decision)]

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            repository._capabilities.update({run_id: "local-secret" for run_id in (*live, "stale")})
            repository._lease_expires.update(
                {run_id: now + timedelta(minutes=1) for run_id in live}
            )
            repository._lease_expires["stale"] = now - timedelta(seconds=1)
            with (
                patch.object(repository, "get_run", side_effect=get_run),
                patch.object(repository, "list_tool_calls", side_effect=list_tool_calls),
            ):
                assert await repository.list_decided_approval_runs() == ["approved"]
            assert not repository.owns_run_lease("stale")

    asyncio.run(exercise())
    assert queried_runs == list(live)
    assert queried_tools == ["approved", "undecided", "completed_tool"]
