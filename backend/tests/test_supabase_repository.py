import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.models import ModelDescriptor, SessionWorkspaceBusy
from app.domain.runtime import (
    ModelCallStatus,
    ModelMessage,
    ModelToolCall,
    RunEventType,
    RunStatus,
    ToolApprovalDecision,
    ToolCallStatus,
)
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


def _model() -> ModelDescriptor:
    return ModelDescriptor(
        id="openai:gpt-5.5",
        provider_id="openai",
        provider_name="OpenAI",
        adapter_kind="openai",
        upstream_model_id="gpt-5.5",
        name="GPT-5.5",
        requires_api_key=True,
        supports_streaming=True,
        supports_tools=True,
        enabled=True,
    )


def _run_row() -> dict[str, object]:
    return {
        "id": "37a7e834-82a5-4635-aa29-06285d6bc8da",
        "user_id": str(ALICE),
        "chat_id": "5f2b6daa-65db-43ad-b0ad-742e5c3645b9",
        "turn_id": "turn-one",
        "status": "queued",
        "provider_id": "openai",
        "model_id": "openai:gpt-5.5",
        "adapter_kind": "openai",
        "upstream_model_id": "gpt-5.5",
        "input_message_id": "8875fe53-9716-41c6-95a7-83c3c87202c1",
        "retry_of": None,
        "client_request_id": "request-one",
        "max_model_calls": 8,
        "max_tool_calls": 8,
        "budget_preset": "conservative",
        "max_total_tokens": 100000,
        "max_cost_usd": 2.0,
        "deadline_at": "2026-10-10T10:10:00+00:00",
        "cancel_requested_at": None,
        "lease_expires_at": "2026-10-10T10:01:00+00:00",
        "recovery_count": 0,
        "stop_reason": None,
        "last_event_sequence": 1,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-10-10T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
    }


def test_verified_token_source_rejects_unverified_identity_and_expiry() -> None:
    source = VerifiedTokenSource(ALICE)
    with pytest.raises(RuntimeError, match="verified token"):
        source.get_token()
    with pytest.raises(ValueError, match="different account"):
        source.record_verified("bob-token", BOB, datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(ValueError, match="expired"):
        source.record_verified("expired-token", ALICE, datetime.now(UTC) - timedelta(seconds=1))
    source.record_verified("alice-token", ALICE, datetime.now(UTC) + timedelta(seconds=4))
    with pytest.raises(RuntimeError, match="expired"):
        source.get_token()


def test_verified_token_source_keeps_freshest_same_account_session() -> None:
    source = VerifiedTokenSource(ALICE)
    now = datetime.now(UTC)
    source.record_verified("older-tab", ALICE, now + timedelta(minutes=10))
    source.record_verified("fresh-tab", ALICE, now + timedelta(hours=1))
    source.record_verified("older-tab", ALICE, now + timedelta(minutes=10))

    assert source.get_token() == "fresh-tab"
    assert source.valid_for(30 * 60)


def test_import_seal_uses_authenticated_rpc() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"sealed": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            await repository.seal_import("private-import-capability-with-forty-characters")

    asyncio.run(exercise())
    assert len(captured) == 1
    assert captured[0].url.path == "/rest/v1/rpc/seal_import"
    assert captured[0].headers["Authorization"] == "Bearer alice-token"
    assert captured[0].headers["Content-Profile"] == "trellis"
    assert json.loads(captured[0].content) == {
        "p_capability": "private-import-capability-with-forty-characters"
    }


def test_import_batch_sends_private_capability_with_rows() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"inserted": 1})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            await repository.import_batch(
                "profiles", [{"id": str(ALICE)}], "private-import-capability-with-forty-characters"
            )

    asyncio.run(exercise())
    assert len(captured) == 1
    assert captured[0].url.path == "/rest/v1/rpc/import_snapshot"
    assert json.loads(captured[0].content) == {
        "p_snapshot": {"table": "profiles", "rows": [{"id": str(ALICE)}]},
        "p_capability": "private-import-capability-with-forty-characters",
    }


def test_profile_read_uses_authenticated_rls_request_and_maps_auth_email_separately() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(ALICE),
                    "display_name": "Alice",
                    "created_at": "2026-10-10T10:00:00+00:00",
                    "updated_at": "2026-10-10T10:00:00+00:00",
                }
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            profile = await repository.get_profile()
            assert profile.id == str(ALICE)
            assert profile.display_name == "Alice"
            assert profile.email is None

    asyncio.run(exercise())

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == "/rest/v1/profiles"
    assert request.url.params["id"] == f"eq.{ALICE}"
    assert request.headers["Authorization"] == "Bearer alice-token"
    assert request.headers["apikey"] == "sb_publishable_test-key"
    assert request.headers["Accept-Profile"] == "trellis"


def test_mutations_use_rpc_and_recheck_latest_verified_token() -> None:
    captured: list[httpx.Request] = []
    token_source = _source()

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=None)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, token_source, client)
            await repository.set_default_budget_preset("longer")
            token_source.record_verified(
                "fresh-alice-token", ALICE, datetime.now(UTC) + timedelta(hours=1)
            )
            await repository.set_default_budget_preset("conservative")

    asyncio.run(exercise())

    assert [request.url.path for request in captured] == [
        "/rest/v1/rpc/mutate",
        "/rest/v1/rpc/mutate",
    ]
    assert [request.headers["Authorization"] for request in captured] == [
        "Bearer alice-token",
        "Bearer fresh-alice-token",
    ]
    assert captured[0].headers["Content-Profile"] == "trellis"
    assert captured[0].content == (
        b'{"p_action":"set_default_budget_preset","p_payload":{"preset":"longer"},'
        b'"p_capability":null}'
    )


def test_run_writes_require_locally_owned_capability_and_never_expose_it_in_rows() -> None:
    captured: list[httpx.Request] = []
    run = _run_row()

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(200, json={"run": run, "lease_acquired": True})
        return httpx.Response(
            200,
            json={
                "user_id": str(ALICE),
                "run_id": run["id"],
                "sequence": 2,
                "event_type": "assistant.delta",
                "event_version": 1,
                "data": {"text": "hi"},
                "created_at": "2026-10-10T10:00:01+00:00",
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            created = await repository.create_run(
                str(run["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            assert created.id == run["id"]
            event = await repository.append_run_event(
                created.id, RunEventType.ASSISTANT_DELTA, {"text": "hi"}
            )
            assert event.sequence == 2
            with pytest.raises(ApplicationError) as missing_lease:
                await repository.append_run_event(
                    "cf6d48d2-dd2b-4d6f-944a-235991d05a99",
                    RunEventType.ASSISTANT_DELTA,
                    {"text": "unauthorized"},
                )
            assert missing_lease.value.code == "run_not_owned"

    asyncio.run(exercise())

    assert len(captured) == 2
    first = captured[0].content.decode()
    second = captured[1].content.decode()
    assert '"p_action":"create_run"' in first
    assert '"p_action":"append_run_event"' in second
    assert '"p_capability":null' not in first
    assert '"p_capability":null' not in second
    assert "lease_token" not in first + second
    assert "alice-token" not in first + second


def test_existing_run_does_not_grant_capability_to_another_process() -> None:
    run = _run_row()
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"run": run, "lease_acquired": False})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            created = await repository.create_run(
                str(run["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            with pytest.raises(ApplicationError) as missing_lease:
                await repository.request_run_cancellation(created.id)
            assert missing_lease.value.code == "run_not_owned"

    asyncio.run(exercise())
    assert len(captured) == 1


def test_settings_and_conversation_reads_map_cloud_rows() -> None:
    chat_id = "5f2b6daa-65db-43ad-b0ad-742e5c3645b9"
    settings_row = {
        "user_id": str(ALICE),
        "selected_provider": "openai",
        "selected_model_id": "openai:gpt-5.5",
        "default_budget_preset": "conservative",
    }
    chat_row = {
        "id": chat_id,
        "user_id": str(ALICE),
        "title": "Hello",
        "workspace_path": None,
        "created_at": "2026-10-10T10:00:00+00:00",
        "updated_at": "2026-10-10T10:00:00+00:00",
    }
    message_row = {
        "id": "8875fe53-9716-41c6-95a7-83c3c87202c1",
        "user_id": str(ALICE),
        "chat_id": chat_id,
        "turn_id": "turn-one",
        "ordinal": 1,
        "role": "user",
        "content": "Hello",
        "provider": None,
        "model": None,
        "created_at": "2026-10-10T10:00:00+00:00",
    }
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-0/1"})
        rows: dict[str, object] = {
            "/rest/v1/user_settings": [settings_row],
            "/rest/v1/models": [
                {
                    "id": "openai:gpt-5.5",
                    "provider_id": "openai",
                    "provider_name": "OpenAI",
                    "adapter_kind": "openai",
                    "upstream_model_id": "gpt-5.5",
                    "name": "GPT-5.5",
                    "requires_api_key": True,
                    "supports_streaming": True,
                    "supports_tools": True,
                    "enabled": True,
                }
            ],
            "/rest/v1/onboarding_progress": [{"user_id": str(ALICE), "current_step": "profile"}],
            "/rest/v1/chats": [chat_row],
            "/rest/v1/messages": [message_row],
        }
        return httpx.Response(200, json=rows[request.url.path])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            assert await repository.get_selected_provider() == "openai"
            assert await repository.get_selected_model_id() == "openai:gpt-5.5"
            assert await repository.get_default_budget_preset() == "conservative"
            assert [model.id for model in await repository.list_models()] == ["openai:gpt-5.5"]
            assert (await repository.get_onboarding_progress()).current_step == "profile"
            assert (await repository.list_sessions())[0].message_count == 1
            session = await repository.get_session(chat_id)
            assert session is not None and session.title == "Hello"
            assert (await repository.list_messages(chat_id))[0].session_id == chat_id
            assert (await repository.get_turn_messages(chat_id, "turn-one"))[0].content == "Hello"

    asyncio.run(exercise())
    assert all(request.headers["Accept-Profile"] == "trellis" for request in calls)
    assert all(
        request.headers["Prefer"] == "count=exact" for request in calls if request.method == "HEAD"
    )
    assert all(
        request.url.params.get("user_id") == f"eq.{ALICE}"
        for request in calls
        if request.url.path not in {"/rest/v1/models"}
    )


def test_unbounded_message_reads_fetch_all_postgrest_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.infrastructure.supabase_repository._READ_PAGE_SIZE", 2, raising=False)
    chat_id = "5f2b6daa-65db-43ad-b0ad-742e5c3645b9"
    offsets: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params["offset"])
        offset = int(request.url.params["offset"])
        count = min(2, 3 - offset)
        return httpx.Response(
            200,
            json=[
                {
                    "id": f"message-{ordinal}",
                    "user_id": str(ALICE),
                    "chat_id": chat_id,
                    "turn_id": "turn-one",
                    "ordinal": ordinal,
                    "role": "user",
                    "content": f"Message {ordinal}",
                    "provider": None,
                    "model": None,
                    "created_at": "2026-10-10T10:00:00+00:00",
                }
                for ordinal in range(offset + 1, offset + count + 1)
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            messages = await repository.list_messages(chat_id)
            assert [message.ordinal for message in messages] == [1, 2, 3]

    asyncio.run(exercise())
    assert offsets == ["0", "2"]


def test_mutation_methods_send_one_sql_transaction_each() -> None:
    captured: list[httpx.Request] = []
    profile = {
        "id": str(ALICE),
        "display_name": "Alice",
        "created_at": "2026-10-10T10:00:00+00:00",
        "updated_at": "2026-10-10T10:00:00+00:00",
    }
    progress = {"user_id": str(ALICE), "current_step": "model"}
    chat_id = "5f2b6daa-65db-43ad-b0ad-742e5c3645b9"
    message = {
        "id": "8875fe53-9716-41c6-95a7-83c3c87202c1",
        "user_id": str(ALICE),
        "chat_id": chat_id,
        "turn_id": "turn-one",
        "ordinal": 1,
        "role": "user",
        "content": "Hello",
        "provider": None,
        "model": None,
        "created_at": "2026-10-10T10:00:00+00:00",
    }
    results: dict[str, object] = {
        "update_profile": profile,
        "advance_onboarding_intro": progress,
        "save_onboarding_profile": {"profile": profile, "progress": progress},
        "complete_onboarding": True,
        "set_selected_provider": "openai",
        "set_selected_model": True,
        "claim_turn": True,
        "release_turn": None,
        "add_user_message": message,
        "add_assistant_message": {
            **message,
            "role": "assistant",
            "provider": "openai",
            "model": "gpt-5.5",
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        action = json.loads(request.content)["p_action"]
        return httpx.Response(200, json=results[action])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            assert (
                await repository.update_profile("Alice", "ignored@example.com")
            ).display_name == "Alice"
            assert (await repository.advance_onboarding_intro()).current_step == "model"
            profile_row, progress_row = await repository.save_onboarding_profile(
                "Alice", "ignored@example.com"
            )
            assert profile_row.email is None and progress_row.current_step == "model"
            assert await repository.complete_onboarding("openai:gpt-5.5")
            assert await repository.set_selected_provider("openai") == "openai"
            assert await repository.set_selected_model("openai:gpt-5.5")
            assert await repository.claim_turn(chat_id, "turn-one")
            assert (await repository.add_user_message(chat_id, "turn-one", "Hello")).role == "user"
            assert (
                await repository.add_assistant_message(
                    chat_id, "turn-one", "Hi", "openai", "gpt-5.5"
                )
            ).role == "assistant"
            await repository.release_turn(chat_id, "turn-one")

    asyncio.run(exercise())
    assert len(captured) == 10
    assert all(request.url.path == "/rest/v1/rpc/mutate" for request in captured)
    assert b"ignored@example.com" not in b"".join(request.content for request in captured)
    turn_capabilities = [json.loads(request.content)["p_capability"] for request in captured[6:]]
    assert len(turn_capabilities[0]) >= 32
    assert len(set(turn_capabilities)) == 1


def test_classic_turn_writes_require_a_private_local_claim_capability() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        action = json.loads(request.content)["p_action"]
        return httpx.Response(200, json=True if action == "claim_turn" else None)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ApplicationError) as missing:
                await repository.add_user_message("chat-one", "turn-one", "Hello")
            assert missing.value.code == "turn_not_owned"
            assert await repository.claim_turn("chat-one", "turn-one")
            await repository.release_turn("chat-one", "turn-one")
            with pytest.raises(ApplicationError) as released:
                await repository.add_user_message("chat-one", "turn-one", "Hello")
            assert released.value.code == "turn_not_owned"

    asyncio.run(exercise())
    assert len(captured) == 2
    claim = json.loads(captured[0].content)
    release = json.loads(captured[1].content)
    assert claim["p_action"] == "claim_turn"
    assert release["p_action"] == "release_turn"
    assert len(claim["p_capability"]) >= 32
    assert release["p_capability"] == claim["p_capability"]


def test_session_mutations_return_chat_with_message_count() -> None:
    chat_id = "5f2b6daa-65db-43ad-b0ad-742e5c3645b9"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/rest/v1/rpc/mutate":
            return httpx.Response(200, json={"id": chat_id, "user_id": str(ALICE)})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "*/0"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": chat_id,
                    "user_id": str(ALICE),
                    "title": "New session",
                    "workspace_path": "/home/alice/project",
                    "created_at": "2026-10-10T10:00:00+00:00",
                    "updated_at": "2026-10-10T10:00:00+00:00",
                }
            ],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            created = await repository.create_session()
            assert created.id == chat_id
            updated = await repository.set_session_workspace(chat_id, "/home/alice/project")
            assert updated is not None and updated.workspace_path == "/home/alice/project"

    asyncio.run(exercise())
    assert [request.method for request in requests] == [
        "POST",
        "GET",
        "HEAD",
        "POST",
        "GET",
        "HEAD",
    ]


def test_run_start_requires_token_valid_through_selected_budget() -> None:
    source = VerifiedTokenSource(ALICE)
    source.record_verified("alice-token", ALICE, datetime.now(UTC) + timedelta(minutes=11))
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run": _run_row(), "lease_acquired": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, source, client)
            with pytest.raises(ApplicationError, match="Refresh your sign-in"):
                await repository.create_run(
                    "5f2b6daa-65db-43ad-b0ad-742e5c3645b9",
                    "turn-one",
                    "request-one",
                    "Hello",
                    _model(),
                    budget_preset="longer",
                )

    asyncio.run(exercise())
    assert requests == []


def test_expired_run_recovery_interrupts_without_acquiring_execution_lease() -> None:
    run = {**_run_row(), "status": "interrupted", "lease_expires_at": None}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=run)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            recovered = await repository.recover_expired_run(str(run["id"]))
            assert recovered.status == "interrupted"
            assert not repository.owns_run_lease(recovered.id)

    asyncio.run(exercise())
    sent = json.loads(requests[0].content)
    assert sent == {
        "p_action": "recover_expired_run",
        "p_payload": {"run_id": run["id"]},
        "p_capability": None,
    }


def test_run_recovery_returns_current_row_if_lease_was_renewed() -> None:
    run = {**_run_row(), "status": "running"}

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=run)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            observed = await repository.recover_expired_run(str(run["id"]))
            assert observed.status is RunStatus.RUNNING
            assert not repository.owns_run_lease(observed.id)

    asyncio.run(exercise())


def test_runtime_reads_map_rls_rows_and_omit_lease_token() -> None:
    run = _run_row()
    run_id = str(run["id"])
    call_id = "d659c7ac-931e-42ba-bb9c-b07946c9b615"
    assistant_id = "0fb6a917-dcdd-490f-a895-0ad0c414528f"
    model_call = {
        "id": call_id,
        "user_id": str(ALICE),
        "run_id": run_id,
        "step_index": 1,
        "provider_id": "openai",
        "model_id": "openai:gpt-5.5",
        "adapter_kind": "openai",
        "status": "completed",
        "request_snapshot": {"messages": []},
        "response_snapshot": {"text": "Hi"},
        "provider_response_id": "response-one",
        "finish_reason": "stop",
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": None,
        "cached_read_tokens": 0,
        "cache_creation_tokens": 0,
        "estimated_cost": 0.01,
        "error_code": None,
        "error_message": None,
        "started_at": "2026-10-10T10:00:00+00:00",
        "finished_at": "2026-10-10T10:00:01+00:00",
    }
    assistant = {
        "id": assistant_id,
        "user_id": str(ALICE),
        "run_id": run_id,
        "ordinal": 1,
        "role": "assistant",
        "content": "Hi",
        "continuation": [
            {"provider_id": "openai", "model_id": "openai:gpt-5.5", "payload_json": "{}"}
        ],
        "model_call_id": call_id,
        "tool_call_id": None,
        "created_at": "2026-10-10T10:00:01+00:00",
    }
    tool = {
        "id": "132f3b78-81c6-40f7-b7d9-c2b3186e5fea",
        "user_id": str(ALICE),
        "run_id": run_id,
        "assistant_message_id": assistant_id,
        "call_index": 0,
        "provider_call_id": "provider-tool-one",
        "name": "read_file",
        "arguments": {"path": "README.md"},
        "status": "pending",
        "approval_decision": None,
        "approval_decided_at": None,
        "approval_preview": None,
        "created_at": "2026-10-10T10:00:01+00:00",
        "finished_at": None,
    }
    event = {
        "user_id": str(ALICE),
        "run_id": run_id,
        "sequence": 1,
        "event_type": "run.queued",
        "event_version": 1,
        "data": {"status": "queued"},
        "created_at": "2026-10-10T10:00:00+00:00",
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        rows = {
            "/rest/v1/runs": [run],
            "/rest/v1/run_events": [event],
            "/rest/v1/model_calls": [model_call],
            "/rest/v1/run_messages": [assistant],
            "/rest/v1/tool_calls": [tool],
        }
        return httpx.Response(200, json=rows[request.url.path])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            one = await repository.get_run(run_id)
            latest = await repository.get_latest_run_for_session(str(run["chat_id"]))
            assert one is not None and one.session_id == run["chat_id"]
            assert latest is not None and latest.id == run_id
            assert [
                item.id
                for item in await repository.list_runs_for_session(str(run["chat_id"]), 0, 20)
            ] == [run_id]
            assert (await repository.list_run_events(run_id))[0].event_type is RunEventType.QUEUED
            assert (await repository.list_model_calls(run_id))[0].cached_tokens == 0
            messages = await repository.list_run_messages(run_id)
            assert messages[0].tool_calls[0].id == "provider-tool-one"
            assert messages[0].continuation_items[0].payload_json == "{}"
            assert (await repository.list_tool_calls(run_id))[0].name == "read_file"

    asyncio.run(exercise())
    assert requests
    assert all("lease_token" not in request.url.params.get("select", "") for request in requests)
    assert all(request.url.params["user_id"] == f"eq.{ALICE}" for request in requests)


def test_runtime_mutations_use_same_local_capability_for_atomic_rpc_actions() -> None:
    run = _run_row()
    run_id = str(run["id"])
    event = {
        "user_id": str(ALICE),
        "run_id": run_id,
        "sequence": 2,
        "event_type": "run.started",
        "event_version": 1,
        "data": {"status": "running"},
        "created_at": "2026-10-10T10:00:01+00:00",
    }
    message = {
        "id": "f1ec3900-ef25-4f3b-bd7d-afd176944ec6",
        "user_id": str(ALICE),
        "chat_id": run["chat_id"],
        "turn_id": "turn-one",
        "ordinal": 2,
        "role": "assistant",
        "content": "Hi",
        "provider": "openai",
        "model": "openai:gpt-5.5",
        "created_at": "2026-10-10T10:00:02+00:00",
    }
    model_call = {
        "id": "d659c7ac-931e-42ba-bb9c-b07946c9b615",
        "user_id": str(ALICE),
        "run_id": run_id,
        "step_index": 1,
        "provider_id": "openai",
        "model_id": "openai:gpt-5.5",
        "adapter_kind": "openai",
        "status": "completed",
        "request_snapshot": {},
        "response_snapshot": {},
        "provider_response_id": None,
        "finish_reason": "stop",
        "input_tokens": 1,
        "output_tokens": 1,
        "reasoning_tokens": None,
        "cached_read_tokens": None,
        "cache_creation_tokens": None,
        "estimated_cost": None,
        "error_code": None,
        "error_message": None,
        "started_at": "2026-10-10T10:00:00+00:00",
        "finished_at": "2026-10-10T10:00:01+00:00",
    }
    run_message = {
        "id": "0fb6a917-dcdd-490f-a895-0ad0c414528f",
        "user_id": str(ALICE),
        "run_id": run_id,
        "ordinal": 1,
        "role": "assistant",
        "content": "Hi",
        "continuation": [],
        "model_call_id": model_call["id"],
        "tool_call_id": None,
        "created_at": "2026-10-10T10:00:01+00:00",
    }
    tool_call = {
        "id": "132f3b78-81c6-40f7-b7d9-c2b3186e5fea",
        "user_id": str(ALICE),
        "run_id": run_id,
        "assistant_message_id": run_message["id"],
        "call_index": 0,
        "provider_call_id": "tool-one",
        "name": "read_file",
        "arguments": {"path": "README.md"},
        "status": "pending",
        "approval_decision": None,
        "approval_decided_at": None,
        "approval_preview": None,
        "created_at": "2026-10-10T10:00:01+00:00",
        "finished_at": None,
    }
    results = {
        "create_run": {"run": run, "lease_acquired": True},
        "transition_run_record": event,
        "complete_run": {"message": message, "events": [event]},
        "create_model_call": model_call,
        "update_model_call": model_call,
        "record_assistant_message": {
            "message": run_message,
            "tool_calls": [tool_call],
            "events": [event],
        },
        "record_tool_result": {"message": run_message, "tool_call": tool_call, "event": event},
        "record_tool_approval_decision": {"tool_call": tool_call, "event": event},
        "request_tool_approval": {"run": run, "event": event},
        "resume_approved_run": {"run": run, "event": event},
        "renew_run_lease": {"run": run, "lease_acquired": True},
    }
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        requests.append(sent)
        return httpx.Response(200, json=results[sent["p_action"]])

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            await repository.create_run(
                str(run["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            assert repository.owns_run_lease(run_id)
            assert (
                await repository.transition_run_record(
                    run_id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
                )
            ).sequence == 2
            assert (await repository.create_model_call(run_id, 1, _model(), {})).step_index == 1
            assert (
                await repository.update_model_call(
                    str(model_call["id"]), ModelCallStatus.COMPLETED, finish_reason="stop"
                )
            ).status == ModelCallStatus.COMPLETED
            recorded, calls, events = await repository.record_assistant_message(
                run_id,
                str(model_call["id"]),
                ModelMessage(
                    role="assistant",
                    content="Hi",
                    tool_calls=(ModelToolCall("tool-one", "read_file", {"path": "README.md"}),),
                ),
            )
            assert recorded.role == "assistant" and len(calls) == 1 and len(events) == 1
            assert (
                await repository.record_tool_result(
                    run_id, str(tool_call["id"]), "done", status=ToolCallStatus.COMPLETED
                )
            )[1].id == tool_call["id"]
            assert (
                await repository.record_tool_approval_decision(
                    run_id, str(tool_call["id"]), ToolApprovalDecision.APPROVED
                )
            )[0].name == "read_file"
            assert (await repository.request_tool_approval(run_id, str(tool_call["id"]), None))[
                1
            ].sequence == 2
            assert (await repository.resume_approved_run(run_id))[1].sequence == 2
            assert (await repository.renew_run_lease(run_id)).id == run_id
            completed_message, completed_events = await repository.complete_run(run_id, "Hi")
            assert completed_message.role == "assistant" and len(completed_events) == 1
            assert not repository.owns_run_lease(run_id)

    asyncio.run(exercise())
    assert len(requests) == 11
    capability = requests[0]["p_capability"]
    assert isinstance(capability, str) and len(capability) >= 40
    assert all(request["p_capability"] == capability for request in requests[1:])


def test_locally_known_expired_lease_cannot_start_another_tool_write() -> None:
    run = {**_run_row(), "lease_expires_at": "2000-01-01T00:00:00+00:00"}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run": run, "lease_acquired": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            created = await repository.create_run(
                str(run["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            assert not repository.owns_run_lease(created.id)
            with pytest.raises(ApplicationError) as missing_lease:
                await repository.append_run_event(
                    created.id, RunEventType.ASSISTANT_DELTA, {"text": "hi"}
                )
            assert missing_lease.value.code == "run_not_owned"

    asyncio.run(exercise())
    assert len(requests) == 1


def test_workspace_busy_conflict_preserves_application_error_type() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"code": "PT409", "message": "session_workspace_busy", "details": None}
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(SessionWorkspaceBusy):
                await repository.set_session_workspace(
                    "5f2b6daa-65db-43ad-b0ad-742e5c3645b9", "/tmp/project"
                )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ("session_run_busy", "session_run_busy"),
        ("session_turn_busy", "session_turn_busy"),
        ("account_import_in_progress", "account_import_in_progress"),
        ("account_activity_busy", "account_activity_busy"),
    ],
)
def test_cloud_mutation_conflicts_have_public_codes(message: str, expected_code: str) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "PT409", "message": message})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ApplicationError) as conflict:
                await repository.set_default_budget_preset("conservative")
            assert conflict.value.code == expected_code

    asyncio.run(exercise())


def test_run_conflict_preserves_existing_public_error_mapping() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "22023", "message": "run request conflicts with a previous payload"},
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            with pytest.raises(ValueError, match="conflicts with a previous payload"):
                await repository.create_run(
                    "5f2b6daa-65db-43ad-b0ad-742e5c3645b9",
                    "turn-one",
                    "request-one",
                    "Hello",
                    _model(),
                )

    asyncio.run(exercise())


def test_successful_lease_renewal_can_finish_after_previous_deadline() -> None:
    first = {
        **_run_row(),
        "lease_expires_at": (datetime.now(UTC) + timedelta(milliseconds=200)).isoformat(),
    }
    renewed = {
        **first,
        "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
    }
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"run": first, "lease_acquired": True})
        await asyncio.sleep(0.25)
        return httpx.Response(200, json={"run": renewed, "lease_acquired": True})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            run = await repository.create_run(
                str(first["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            assert repository.owns_run_lease(run.id)
            assert (await repository.renew_run_lease(run.id)).id == run.id
            assert repository.owns_run_lease(run.id)

    asyncio.run(exercise())


def test_rejected_lease_renewal_revokes_local_execution_immediately() -> None:
    run = _run_row()
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"run": run, "lease_acquired": True})
        return httpx.Response(
            403, json={"code": "42501", "message": "run capability invalid or expired"}
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, _source(), client)
            created = await repository.create_run(
                str(run["chat_id"]), "turn-one", "request-one", "Hello", _model()
            )
            assert repository.owns_run_lease(created.id)
            with pytest.raises(ApplicationError):
                await repository.renew_run_lease(created.id)
            assert not repository.owns_run_lease(created.id)

    asyncio.run(exercise())
