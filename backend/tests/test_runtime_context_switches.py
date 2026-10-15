import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.application.context_budget import input_fingerprint
from app.application.runs import RunService
from app.domain.capabilities import RuntimeCapabilities
from app.domain.context import InputTokenCount, NativeCompactionResult
from app.domain.runtime import ModelStreamEvent, RunStatus
from app.infrastructure.secrets import SecretStore
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from tests.memory_repository import MemoryRepository
from tests.test_runtime_compaction import CompactionProvider


@pytest.mark.parametrize(
    "change",
    ["same", "model", "provider", "instructions", "workspace", "small_window", "large_window"],
)
def test_checkpoint_replay_survives_switches_without_loading_archived_transcript(
    tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BoundedRepository(MemoryRepository):
        async def list_messages(self, session_id):
            raise AssertionError("Runtime must use the atomic unarchived context window")

    class PortableNativeProvider(CompactionProvider):
        def __init__(self):
            super().__init__()
            self.switched_window = False
            self.summary_count = 0

        async def count_input(self, request, api_key, user_id):
            summary = request.system_instructions.startswith("Summarize the historical")
            tokens = (
                30000 if self.switched_window and self.summary_count == 1 and not summary else 100
            )
            return InputTokenCount(tokens, "provider", input_fingerprint(request))

        async def compact(self, request, api_key, user_id):
            return NativeCompactionResult(
                "openai_window",
                '[{"type":"compaction","encrypted_content":"sealed-history"}]',
                (ModelStreamEvent("usage", input_tokens=12, output_tokens=5, cached_tokens=0),),
                "native-response",
            )

        def stream(self, request, api_key, user_id):
            self.requests.append(request)

            async def generate():
                summary = request.system_instructions.startswith("Summarize the historical")
                if summary:
                    self.summary_count += 1
                yield ModelStreamEvent(
                    "text_delta", text="Keep tea preference." if summary else "Answer"
                )
                yield ModelStreamEvent(
                    "usage",
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=0,
                    cache_creation_tokens=0,
                )
                yield ModelStreamEvent("completed", finish_kind="final")

            return generate()

    root_a = (tmp_path / "a").resolve()
    root_b = (tmp_path / "b").resolve()
    root_a.mkdir()
    root_b.mkdir()

    async def exercise():
        database = BoundedRepository(tmp_path / "db")
        session = await database.create_session(str(root_a))
        bindings = LocalWorkspaceBindings()
        bindings.bind(session.id, root_a)
        guidance = "Original workspace rules."

        async def read_guidance(_root):
            return guidance

        for index in range(65):
            await database.add_user_message(session.id, str(index), f"Archived marker {index}")
            await database.add_assistant_message(
                session.id, str(index), "Done", "openai", "openai:gpt-5.5"
            )
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        await secrets.set("anthropic", "test-key")
        provider = PortableNativeProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider, "anthropic": provider},
            workspace_bindings=bindings,
            workspace_guidance_reader=read_guidance,
            capabilities=RuntimeCapabilities(frozenset({"local_read"})),
        )
        try:
            first = await service.create_run(
                session.id, "first", "Remember my tea preference", budget_preset="longer"
            )
            await service.wait_for_run(first.id)
            checkpoint = await database.load_context_checkpoint(session.id)
            assert checkpoint is not None and checkpoint.draft.native_payload_json is not None
            target = "openai:gpt-5.5"
            if change in {"model", "small_window", "large_window"}:
                target = "openai:gpt-6-luna"
                database.state.models.append(
                    replace(database.state.models[0], id=target, upstream_model_id="gpt-6-luna")
                )
                await database.set_session_model(session.id, target)
                if change in {"small_window", "large_window"}:
                    from app.application.model_catalog import profile_for_model

                    original = profile_for_model(target)
                    assert original is not None
                    fixture = replace(
                        original,
                        context_window_tokens=40000 if change == "small_window" else 2000000,
                        max_output_tokens=8192,
                        supports_native_compaction=False,
                    )
                    monkeypatch.setattr("app.application.runs.profile_for_model", lambda _: fixture)
                    provider.switched_window = True
            elif change == "provider":
                target = "anthropic:claude-sonnet-5"
                await database.set_session_model(session.id, target)
            elif change == "instructions":
                guidance = "Changed workspace rules."
            elif change == "workspace":
                await database.set_session_workspace(session.id, str(root_b))
                bindings.bind(session.id, root_b)
            second = await service.create_run(session.id, "second", "What do I prefer?")
            await service.wait_for_run(second.id)
            saved = await database.get_run(second.id)
            assert saved is not None and saved.status is RunStatus.COMPLETED
            assert saved.model_id == target
            request = provider.requests[-1]
            assert request.model_id == target
            assert request.messages[-1].content == "What do I prefer?"
            assert all("Archived marker 0" not in message.content for message in request.messages)
            if change == "same":
                assert request.native_context_json == checkpoint.draft.native_payload_json
            else:
                assert request.native_context_json is None
                assert request.messages[0].content.endswith("Keep tea preference.")
                assert "sealed-history" not in str(request)
            current_checkpoint = await database.load_context_checkpoint(session.id)
            calls = await database.list_model_calls(second.id)
            assert all(call.model_id == target for call in calls)
            replay = calls[-1].request_snapshot["context"]
            assert isinstance(replay, dict)
            assert replay["checkpoint_replay"] == ("native" if change == "same" else "portable")
            if change == "small_window":
                assert current_checkpoint is not None
                assert current_checkpoint.revision == checkpoint.revision + 1
                assert current_checkpoint.parent_id == checkpoint.id
                assert database.state.context_checkpoints[checkpoint.id] == checkpoint
                assert len(calls) == 2 and calls[0].request_snapshot["purpose"] == "summary"
            else:
                assert current_checkpoint == checkpoint
                assert len(calls) == 1
            if change == "large_window":
                context = calls[0].request_snapshot["context"]
                assert isinstance(context, dict)
                assert context["input_tokens"] == 30000
                assert context["needs_compaction"] is False
                assert provider.summary_count == 1
            previous = await database.get_run(first.id)
            assert previous is not None and previous.model_id == "openai:gpt-5.5"
        finally:
            await service.close()

    asyncio.run(exercise())
