import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.application.context_budget import input_fingerprint
from app.application.errors import ProviderError
from app.application.runs import RunService
from app.domain.context import InputTokenCount
from app.domain.runtime import RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.test_runtime_compaction import CompactionProvider


@pytest.mark.parametrize("count_fails", [False, True])
def test_runtime_uses_bounded_exact_counts_and_estimates_when_counting_fails(
    tmp_path: Path, count_fails: bool
) -> None:
    class CountedProvider(CompactionProvider):
        def __init__(self):
            super().__init__()
            self.count_requests = []

        async def count_input(self, request, api_key, user_id):
            self.count_requests.append(request)
            if count_fails:
                raise ProviderError("provider_upstream_failed", "Count unavailable")
            return InputTokenCount(100, "provider", input_fingerprint(request))

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = CountedProvider()
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            assert len(provider.count_requests) == 1
            call = (await database.list_model_calls(run.id))[0]
            context = call.request_snapshot["context"]
            assert isinstance(context, dict)
            assert context["count_method"] == (
                "utf8_bytes_estimate_v1" if count_fails else "provider"
            )
            assert context["input_hash"] == input_fingerprint(provider.count_requests[0])
            assert context["input_hash"] == input_fingerprint(provider.requests[0])
            if not count_fails:
                assert context["input_tokens"] == 100
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "invalid",
        "unknown",
        "error",
        "replay_count_error",
        "replay_count_mismatch",
        "unknown_tier",
    ],
)
def test_native_compaction_accounts_each_iteration_and_preserves_portable_fallback(
    tmp_path: Path, outcome: str
) -> None:
    import json

    from app.domain.context import NativeCompactionResult
    from app.domain.runtime import ModelStreamEvent

    class NativeProvider(CompactionProvider):
        def __init__(self):
            super().__init__()
            self.compact_requests = []

        async def count_input(self, request, api_key, user_id):
            if request.native_context_json is not None and outcome == "replay_count_error":
                raise ProviderError("provider_upstream_failed", "Count unavailable")
            return InputTokenCount(
                100,
                "provider",
                "0" * 64
                if request.native_context_json is not None and outcome == "replay_count_mismatch"
                else input_fingerprint(request),
            )

        async def compact(self, request, api_key, user_id):
            self.compact_requests.append(request)
            if outcome == "error":
                raise ProviderError("provider_upstream_failed", "Compaction failed")
            usage = tuple(
                ModelStreamEvent(
                    "usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                    service_tier=tier,
                )
                for input_tokens, output_tokens, tier in [
                    (300, 5, "secret-tier-value" if outcome == "unknown_tier" else "batch"),
                    (18, 3, "standard"),
                ]
            )
            return NativeCompactionResult(
                "anthropic_block",
                json.dumps({"type": "compaction", "content": "Native tea.", "signature": "signed"})
                if outcome in {"success", "replay_count_error", "replay_count_mismatch"}
                else None,
                usage,
                "native-response",
                {"invalid": "native_context_invalid", "unknown": "native_usage_unknown"}.get(
                    outcome
                ),
            )

        def stream(self, request, api_key, user_id):
            self.requests.append(request)

            async def generate():
                summary = request.system_instructions.startswith("Summarize the historical")
                yield ModelStreamEvent("text_delta", text="Remember tea." if summary else "Answer")
                yield ModelStreamEvent(
                    "usage",
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                )
                yield ModelStreamEvent("completed", finish_kind="final")

            return generate()

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        await database.set_selected_model("anthropic:claude-sonnet-5")
        session = await database.create_session()
        for index in range(65):
            await database.add_user_message(session.id, str(index), "Old")
            await database.add_assistant_message(
                session.id, str(index), "Done", "anthropic", "anthropic:claude-sonnet-5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("anthropic", "test-key")
        provider = NativeProvider()
        service = RunService(
            database, database, database, database, secrets, {"anthropic": provider}
        )
        try:
            run = await service.create_run(session.id, "current", "Current request")
            await service.wait_for_run(run.id)
            assert len(provider.compact_requests) == 1
            assert all(
                message.content != "Current request"
                for message in provider.compact_requests[0].messages
            )
            calls = await database.list_model_calls(run.id)
            assert calls[0].request_snapshot["purpose"] == "native_compaction"
            saved = await database.get_run(run.id)
            assert saved is not None
            if outcome in {"unknown", "error", "unknown_tier"}:
                assert saved.status is RunStatus.FAILED
                assert len(calls) == 1 and calls[0].status.value == "failed"
                assert calls[0].estimated_cost is None
                assert "secret-tier-value" not in str(calls[0].response_snapshot)
                if outcome == "unknown_tier":
                    assert service._budget_totals(run.model_id, calls) == (326, None)
                else:
                    with pytest.raises(ProviderError, match="enforce this run's budget"):
                        service._budget_totals(run.model_id, calls)
                assert provider.requests == []
                assert await database.load_context_checkpoint(session.id) is None
                return
            assert saved.status is RunStatus.COMPLETED
            assert [call.request_snapshot.get("purpose", "generation") for call in calls] == [
                "native_compaction",
                "summary",
                "generation",
            ]
            assert calls[0].input_tokens == 318 and calls[0].output_tokens == 8
            assert calls[0].estimated_cost == pytest.approx(0.000391)
            assert service._budget_totals(run.model_id, calls)[0] == 360
            checkpoint = await database.load_context_checkpoint(session.id)
            assert checkpoint is not None and checkpoint.draft.summary == "Remember tea."
            assert checkpoint.draft.pricing_context_tokens == 300
            native_request = provider.requests[-1]
            assert native_request.messages[-1].content == "Current request"
            if outcome == "success":
                assert native_request.native_context_json == checkpoint.draft.native_payload_json
                assert native_request.native_context_json is not None
                assert "signed" in native_request.native_context_json
            else:
                assert native_request.native_context_json is None
                assert native_request.messages[0].content.endswith("Remember tea.")
            assert [
                output.content for output in await database.list_assistant_outputs(session.id)
            ] == ["Answer"]
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("longer", [False, True])
@pytest.mark.parametrize("small_window", [False, True])
def test_openai_native_compaction_reserves_uncapped_output_before_dispatch(
    tmp_path: Path, longer: bool, small_window: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.tools import ToolRegistry
    from app.domain.capabilities import RuntimeCapabilities
    from app.domain.context import NativeCompactionResult
    from app.domain.runtime import ModelStreamEvent
    from app.infrastructure.local_tools import LocalReadToolExecutor
    from app.infrastructure.workspace_bindings import LocalWorkspaceBindings

    if small_window:
        from app.application.model_catalog import profile_for_model

        profile = profile_for_model("openai:gpt-5.5")
        assert profile is not None
        monkeypatch.setattr(
            "app.application.execution.profile_for_model",
            lambda _: replace(profile, context_window_tokens=129000),
        )

    workspace_root = tmp_path.resolve()
    canonical_window = (
        '[{"type":"reasoning","encrypted_content":"opaque"},'
        '{"type":"compaction","encrypted_content":"sealed"}]'
    )

    class OpenAICompactProvider(CompactionProvider):
        def __init__(self):
            super().__init__()
            self.compact_requests = []

        async def count_input(self, request, api_key, user_id):
            return InputTokenCount(100, "provider", input_fingerprint(request))

        async def compact(self, request, api_key, user_id):
            self.compact_requests.append(request)
            return NativeCompactionResult(
                "openai_window",
                canonical_window,
                (ModelStreamEvent("usage", input_tokens=12, output_tokens=5, cached_tokens=0),),
                "native-response",
            )

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session(str(workspace_root))
        bindings = LocalWorkspaceBindings()
        bindings.bind(session.id, workspace_root)
        capabilities = RuntimeCapabilities(frozenset({"local_read"}))
        for index in range(65):
            await database.add_user_message(session.id, str(index), "Old")
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        provider = OpenAICompactProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            workspace_bindings=bindings,
            capabilities=capabilities,
            tool_registry=ToolRegistry(LocalReadToolExecutor(), capabilities=capabilities),
        )
        try:
            run = await service.create_run(
                session.id,
                "current",
                "Current request",
                budget_preset="longer" if longer else "conservative",
            )
            await service.wait_for_run(run.id)
            saved = await database.get_run(run.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            assert len(provider.compact_requests) == (1 if longer and not small_window else 0)
            if longer and not small_window:
                call = (await database.list_model_calls(run.id))[0]
                context = call.request_snapshot["context"]
                assert isinstance(context, dict)
                assert context["output_reserve_tokens"] == 128000
                assert provider.compact_requests[0].max_output_tokens == 128000
                assert provider.compact_requests[0].tools == ()
                assert provider.requests[-1].tools
                assert provider.requests[-1].native_context_json == canonical_window
                generation = (await database.list_model_calls(run.id))[-1]
                assert generation.request_snapshot["native_context_json"] == canonical_window
            else:
                assert provider.requests[-1].native_context_json is None
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_private_call_setup_failure_finishes_call_and_preserves_previous_checkpoint(
    tmp_path: Path, native: bool, cancel: bool
) -> None:
    from app.application.model_catalog import profile_for_model
    from app.application.run_context import RunContext
    from app.application.secret_redaction import COMMON_SECRET_REDACTOR
    from app.domain.runtime import RunEventType

    class NativeSetupProvider(CompactionProvider):
        async def count_input(self, request, api_key, user_id):
            return InputTokenCount(100, "provider", input_fingerprint(request))

        async def compact(self, request, api_key, user_id):
            raise AssertionError("Phase publication must happen before native dispatch")

    async def exercise():
        database = MemoryRepository(tmp_path / "db")
        session = await database.create_session()
        for index in range(65):
            await database.add_user_message(session.id, str(index), "Old")
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        service = RunService(
            database, database, database, database, secrets, {"openai": CompactionProvider()}
        )
        try:
            first = await service.create_run(session.id, "first", "First current request")
            await service.wait_for_run(first.id)
        finally:
            await service.close()
        checkpoint = await database.load_context_checkpoint(session.id)
        assert checkpoint is not None
        for index in range(65):
            await database.add_user_message(session.id, f"new-{index}", "More history")
            await database.add_assistant_message(
                session.id, f"new-{index}", "Done", "openai", "openai:gpt-5.5"
            )
        model = (await database.list_models())[0]
        profile = profile_for_model(model.id)
        assert profile is not None
        run = await database.create_run(
            session.id, "next", "next", "Current request", model, budget_preset="longer"
        )
        await database.transition_run_record(run.id, RunStatus.RUNNING, RunEventType.STARTED, {})

        async def failed_publish(_event):
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("Publisher failed")

        provider = NativeSetupProvider() if native else CompactionProvider()
        runtime = RunContext(
            database,
            database,
            database,
            RunService._budget_totals,
            failed_publish,
            COMMON_SECRET_REDACTOR,
        )
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await runtime.prepare(
                run,
                model,
                profile,
                provider,
                "key",
                database.state.user_id,
                workspace_root=None,
                guidance=None,
                tools=(),
            )
        calls = await database.list_model_calls(run.id)
        assert len(calls) == 1
        assert calls[0].request_snapshot["purpose"] == (
            "native_compaction" if native else "summary"
        )
        assert calls[0].status.value == ("cancelled" if cancel else "failed")
        assert calls[0].finished_at is not None
        assert provider.requests == []
        assert await database.load_context_checkpoint(session.id) == checkpoint

    asyncio.run(exercise())
