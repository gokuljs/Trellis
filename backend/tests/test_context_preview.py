import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient as RawTestClient

from app.application.context import build_model_context
from app.application.context_budget import estimate_input_tokens
from app.application.context_preview import ContextPreviewService
from app.application.errors import ApplicationError
from app.application.secret_redaction import SecretRedactor
from app.application.tools import ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.context import ContextWindow
from app.domain.models import Message
from app.domain.runtime import ModelMessage, ModelRequest
from app.infrastructure.local_tools import LocalReadToolExecutor
from tests.memory_repository import MemoryRepository, memory_repository_factory
from tests.support import TestClient, account_context, create_app, make_settings
from tests.test_authenticated_api import StubVerifier
from tests.test_context_checkpoints import fixture

MODEL = "openai:gpt-5.5"
PAYLOAD = {"model_id": MODEL, "draft": "Hello", "budget_preset": "conservative"}


class Credentials:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured

    async def get(self, provider: str) -> str | None:
        raise AssertionError("A preview must not retrieve provider credentials")

    async def status(self, provider: str) -> tuple[bool, str | None]:
        return self.configured, None


def service(database: MemoryRepository, **kwargs: Any) -> ContextPreviewService:

    return ContextPreviewService(
        database,
        database,
        database,
        Credentials(),
        available_adapters=frozenset({"openai", "anthropic"}),
        **kwargs,
    )


def test_preview_includes_system_and_current_draft_without_provider_calls(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path)
        model = (await database.list_models())[0]
        context = build_model_context([], [], workspace_root=None, tools=())
        request = ModelRequest(
            model.provider_id,
            model.id,
            model.adapter_kind,
            model.upstream_model_id,
            (ModelMessage("user", "Hello"),),
            4096,
            system_instructions=context.system_instructions,
        )
        result = await service(database).preview(
            model_id=MODEL, draft="Hello", budget_preset="conservative"
        )
        assert result.model_id == MODEL
        assert result.input_tokens == estimate_input_tokens(request).tokens
        assert result.count_method == "utf8_bytes_estimate_v1"
        assert result.status == "ready"
        assert result.output_reserve_tokens == 4096
        assert result.usable_input_tokens == 100_000 - 4096
        assert result.compaction_threshold_tokens == result.usable_input_tokens * 4 // 5
        assert result.context_window_tokens is not None
        assert result.percentage == pytest.approx(
            result.input_tokens * 100 / result.context_window_tokens
        )
        assert result.checkpoint_id is None
        assert await database.list_sessions() == []

    asyncio.run(exercise())


def test_preview_uses_only_atomic_bounded_suffix_and_portable_checkpoint(tmp_path: Path) -> None:
    async def exercise() -> None:
        class BoundedRepository(MemoryRepository):
            async def list_messages(self, session_id: str) -> list[Message]:
                raise AssertionError("Preview must not read unbounded history")

            async def load_context_window(
                self, session_id: str, *, limit: int = 128
            ) -> ContextWindow:
                assert limit == 128
                return await super().load_context_window(session_id, limit=limit)

        database = BoundedRepository(tmp_path)
        session_id, run_id, candidate = await fixture(database)
        archived = "archived text " * 1000
        database.state.messages[session_id][0] = replace(
            database.state.messages[session_id][0], content=archived
        )
        checkpoint = await database.save_context_checkpoint(
            run_id,
            0,
            replace(
                candidate,
                summary="Private earlier summary",
                native_kind="openai_window",
                native_payload_json=json.dumps(
                    [{"type": "compaction", "encrypted_content": "opaque" * 5000}]
                ),
            ),
        )
        before = dict(database.state.context_checkpoints)
        result = await service(database).preview(
            model_id=MODEL, draft="Hello", budget_preset="conservative", session_id=session_id
        )
        assert result.checkpoint_id == checkpoint.id
        assert result.input_tokens is not None
        assert result.input_tokens < len(archived)
        assert "summary" not in asdict(result)
        assert database.state.context_checkpoints == before
        assert len(database.state.messages[session_id]) == 3
        changed = await service(database).preview(
            model_id=MODEL, draft="Hello🙂", budget_preset="conservative", session_id=session_id
        )
        assert changed.input_tokens == result.input_tokens + len("🙂".encode())
        for index in range(200):
            await database.add_user_message(session_id, f"history-{index}", "small")
        bounded = await service(database).preview(
            model_id=MODEL, draft="Hello", budget_preset="conservative", session_id=session_id
        )
        assert bounded.status == "compaction_required"
        assert bounded.input_tokens is not None and bounded.input_tokens < 20_000

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode",
    [
        "unknown",
        "disabled",
        "pending",
        "unavailable",
        "wrong_upstream",
        "missing_adapter",
        "missing_key",
    ],
)
def test_unavailable_preview_never_borrows_model_limits(tmp_path: Path, mode: str) -> None:
    async def exercise() -> None:
        from app.application.context_preview import ContextPreviewService

        database = MemoryRepository(tmp_path)
        model = database.state.models[0]
        changes = {
            "disabled": {"enabled": False},
            "pending": {"metadata_status": "pending"},
            "unavailable": {"availability": "unavailable"},
            "wrong_upstream": {"upstream_model_id": "untrusted-model"},
        }
        if mode in changes:
            database.state.models[0] = replace(model, **changes[mode])
        preview = ContextPreviewService(
            database,
            database,
            database,
            Credentials(mode != "missing_key"),
            available_adapters=frozenset() if mode == "missing_adapter" else frozenset({"openai"}),
        )
        result = await preview.preview(
            model_id="unknown" if mode == "unknown" else MODEL,
            draft="Hello",
            budget_preset="conservative",
        )
        assert result.status == "unavailable"
        assert result.input_tokens is None
        assert result.context_window_tokens is None
        assert result.percentage is None

    asyncio.run(exercise())


def test_preview_uses_actual_tools_guidance_and_redacts_all_context(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = MemoryRepository(tmp_path / "database")
        session = await database.create_session(str(tmp_path))
        redactor = SecretRedactor("private-value-that-must-not-influence-count" * 50)
        capabilities = RuntimeCapabilities(frozenset({"local_read"}))
        tools = ToolRegistry(LocalReadToolExecutor(), capabilities=capabilities, redactor=redactor)

        class Bindings:
            async def resolve(self, _session: str, _path: str | None) -> Path:
                return tmp_path

        async def guidance(_root: Path) -> str:
            return "Read project guidance."

        preview = service(
            database,
            tool_registry=tools,
            capabilities=capabilities,
            redactor=redactor,
            workspace_bindings=Bindings(),
            workspace_guidance_reader=guidance,
        )
        secret = redactor.streaming_secrets[0]
        result = await preview.preview(
            model_id=MODEL, draft=secret, budget_preset="conservative", session_id=session.id
        )
        safe = await preview.preview(
            model_id=MODEL, draft="[REDACTED]", budget_preset="conservative", session_id=session.id
        )
        bare = await service(database).preview(
            model_id=MODEL, draft="[REDACTED]", budget_preset="conservative", session_id=session.id
        )
        assert result.input_tokens == safe.input_tokens
        assert result.input_tokens is not None and bare.input_tokens is not None
        assert result.input_tokens > bare.input_tokens

    asyncio.run(exercise())


def test_preview_unknown_session_is_not_hidden_by_unknown_model(tmp_path: Path) -> None:
    async def exercise() -> None:
        with pytest.raises(ApplicationError) as caught:
            await service(MemoryRepository(tmp_path)).preview(
                model_id="unknown",
                draft="Hello",
                budget_preset="conservative",
                session_id=str(uuid4()),
            )
        assert caught.value.code == "session_not_found"

    asyncio.run(exercise())


def test_preview_api_auth_ownership_and_no_model_selection_mutation(tmp_path: Path) -> None:
    app = create_app(
        make_settings(tmp_path, openai_api_key="test-only", anthropic_api_key="test-only"),
        auth_verifier=StubVerifier(),
        repository_factory=memory_repository_factory,
    )
    with RawTestClient(app) as client:
        assert client.post("/api/context-preview", json=PAYLOAD).status_code == 401
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        session = client.post("/api/sessions", headers=alice).json()
        payload = {**PAYLOAD, "session_id": session["id"], "model_id": "anthropic:claude-sonnet-5"}
        assert client.post("/api/context-preview", headers=bob, json=payload).status_code == 404
        response = client.post("/api/context-preview", headers=alice, json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["model_id"] == "anthropic:claude-sonnet-5"
        assert client.get(f"/api/sessions/{session['id']}", headers=alice).json()["messages"] == []
        assert client.get("/api/settings", headers=alice).json()["selected_model_id"] == MODEL


def test_preview_api_32k_unicode_scalar_limit_is_not_byte_limit(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, openai_api_key="test-only"))
    with TestClient(app) as client:
        accepted = client.post("/api/context-preview", json={**PAYLOAD, "draft": "🙂" * 32_000})
        assert accepted.status_code == 200
        assert accepted.json()["input_tokens"] > 128_000
        assert accepted.json()["status"] == "compaction_required"
        for draft in ("x" * 32_001, "🙂" * 32_001, "\ud800"):
            response = client.post(
                "/api/context-preview",
                content=json.dumps({**PAYLOAD, "draft": draft}),
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 422
            assert "draft" not in response.text
        assert client.post("/api/context-preview", json={"model_id": MODEL}).status_code == 422
        assert cast(MemoryRepository, account_context(app).database).state.messages == {}


def test_longer_preset_reserves_the_same_output_and_larger_input_budget(tmp_path: Path) -> None:
    async def exercise() -> None:
        preview = service(MemoryRepository(tmp_path))
        small = await preview.preview(model_id=MODEL, draft="", budget_preset="conservative")
        large = await preview.preview(model_id=MODEL, draft="", budget_preset="longer")
        assert small.input_tokens == large.input_tokens
        assert small.output_reserve_tokens == large.output_reserve_tokens == 4096
        assert small.usable_input_tokens == 95_904
        assert large.usable_input_tokens == 195_904

    asyncio.run(exercise())


def test_run_context_metrics_share_the_preview_dto_without_private_sources() -> None:
    from app.application.context_budget import preflight_context
    from app.application.context_preview import preview_from_plan
    from tests.test_context_budget import item, profile, request

    items = (item("m1", "t1", "user", "Hello", required=True),)
    plan = preflight_context(
        request(items), items=items, profile=profile(), remaining_run_tokens=100_000
    )
    ready = preview_from_plan(plan, context_window_tokens=10_000)
    assert ready.status == "ready"
    counted = preview_from_plan(
        replace(plan, input_tokens=12_000, count_method="provider", needs_compaction=True),
        context_window_tokens=10_000,
    )
    assert counted.count_method == "provider" and counted.percentage == 100
    assert counted.status == "compaction_required"
    compacting = preview_from_plan(plan, context_window_tokens=10_000, status="compacting")
    assert compacting.status == "compacting"
    assert compacting.snapshot() == asdict(compacting)
    assert "sources" not in compacting.snapshot() and "input_hash" not in compacting.snapshot()
