import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from app.application.errors import ApplicationError
from app.application.runs import RunService
from app.application.tools import ToolExecutionError, ToolRegistry
from app.domain.capabilities import RuntimeCapabilities, RuntimeCapability
from app.domain.runtime import (
    ModelCallStatus,
    ModelMessage,
    ModelStreamEvent,
    ModelToolCall,
    RunEventType,
    RunStatus,
    ToolApprovalDecision,
)
from app.infrastructure.command_tools import LocalCommandToolExecutor
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor
from app.infrastructure.secrets import SecretStore
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from tests.memory_repository import MemoryRepository
from tests.test_runtime_service import SequencedProvider

LOCAL_CAPABILITIES = RuntimeCapabilities(frozenset({"local_read", "local_patch", "local_command"}))


@pytest.mark.parametrize(
    ("capability", "offered_tool"),
    [("local_patch", "apply_patch"), ("local_command", "run_command")],
)
def test_partial_local_policy_does_not_read_workspace_guidance(
    tmp_path: Path, capability: RuntimeCapability, offered_tool: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Private workspace guidance marker.", encoding="utf-8")
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    policy = RuntimeCapabilities(frozenset({capability}))
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(kind="text_delta", text="Ready."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def unavailable_guidance(workspace_root: Path) -> str | None:
        raise AssertionError("Reading workspace guidance requires local_read permission")

    async def exercise() -> None:
        session = await database.create_session(str(project))
        bindings = LocalWorkspaceBindings()
        bindings.bind(session.id, project)
        await secrets.set("openai", "sk-test-key")
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            capabilities=policy,
            workspace_bindings=bindings,
            workspace_guidance_reader=unavailable_guidance,
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(),
                patch_executor=LocalPatchToolExecutor(),
                command_executor=LocalCommandToolExecutor(),
                capabilities=LOCAL_CAPABILITIES,
            ),
        )
        try:
            run = await service.create_run(session.id, "partial-policy", "Hello")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert [tool.name for tool in provider.requests[0].tools] == [offered_tool]
            assert (
                "Private workspace guidance marker" not in provider.requests[0].system_instructions
            )
        finally:
            await service.close()

    asyncio.run(exercise())


def test_explicit_local_policy_is_intersected_at_catalog_preview_and_execution(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(
        LocalReadToolExecutor(),
        patch_executor=LocalPatchToolExecutor(),
        command_executor=LocalCommandToolExecutor(),
        capabilities=LOCAL_CAPABILITIES,
    )
    call = ModelToolCall(
        "patch", "apply_patch", {"path": "note.txt", "old_text": "", "new_text": "x"}
    )
    assert len(registry.specs(tmp_path)) == 6
    assert registry.specs(tmp_path, capabilities=RuntimeCapabilities()) == ()
    assert registry.specs(None) == ()
    with pytest.raises(ToolExecutionError, match="disabled"):
        asyncio.run(registry.approval_preview(call, tmp_path, capabilities=RuntimeCapabilities()))
    result = asyncio.run(
        registry.execute(call, tmp_path, approved=True, capabilities=RuntimeCapabilities())
    )
    assert result.error_code == "capability_disabled"
    assert not (tmp_path / "note.txt").exists()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_files", {}),
        ("search_files", {"query": "private"}),
        ("read_file", {"path": "note.txt"}),
        ("inspect_git", {"operation": "status"}),
        ("apply_patch", {"path": "note.txt", "old_text": "private", "new_text": "changed"}),
        ("run_command", {"command": "touch marker"}),
    ],
)
def test_chat_runtime_rejects_forged_local_calls_and_skips_workspace_access(
    tmp_path: Path, name: str, arguments: dict[str, object]
) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    (tmp_path / "note.txt").write_text("private", encoding="utf-8")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call", tool_call=ModelToolCall("forged", name, arguments)
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
        )
    )

    class UnavailableBindings:
        def bind(self, session_id: str, workspace_root: Path | None) -> None:
            raise AssertionError("Chat-only runtime must not bind a folder")

        async def resolve(self, session_id: str, workspace_path: str | None) -> Path | None:
            raise AssertionError("Chat-only runtime must not inspect a folder")

    async def unavailable_guidance(root: Path) -> str | None:
        raise AssertionError("Chat-only runtime must not read guidance")

    async def exercise() -> None:
        session = await database.create_session(str(tmp_path))
        await secrets.set("openai", "sk-test-key")
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(),
                patch_executor=LocalPatchToolExecutor(),
                command_executor=LocalCommandToolExecutor(),
                capabilities=LOCAL_CAPABILITIES,
            ),
            workspace_bindings=UnavailableBindings(),
            workspace_guidance_reader=unavailable_guidance,
        )
        try:
            run = await service.create_run(session.id, "forged-call", "Inspect this")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.FAILED
            assert finished.error_code == "capability_disabled"
            assert provider.requests[0].tools == ()
            assert await database.list_tool_calls(run.id) == []
        finally:
            await service.close()

    asyncio.run(exercise())
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "private"
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("saved_decision", [False, True])
def test_disabled_policy_cannot_approve_or_resume_saved_local_calls(
    tmp_path: Path, saved_decision: bool
) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    (tmp_path / "note.txt").write_text("before", encoding="utf-8")
    provider = SequencedProvider(())
    call = ModelToolCall(
        "saved-patch",
        "apply_patch",
        {"path": "note.txt", "old_text": "before", "new_text": "after"},
    )

    async def exercise() -> None:
        session = await database.create_session(str(tmp_path))
        await secrets.set("openai", "sk-test-key")
        model = (await database.list_models())[0]
        run = await database.create_run(session.id, "turn", "saved-local", "Patch the file", model)
        await database.transition_run_record(
            run.id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
        )
        model_call = await database.create_model_call(run.id, 1, model, {})
        await database.update_model_call(
            model_call.id,
            ModelCallStatus.COMPLETED,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.00001,
        )
        _message, calls, _events = await database.record_assistant_message(
            run.id, model_call.id, ModelMessage("assistant", "Patching", tool_calls=(call,))
        )
        await database.request_tool_approval(run.id, calls[0].id, {"diff": "saved"})
        if saved_decision:
            await database.record_tool_approval_decision(
                run.id, calls[0].id, ToolApprovalDecision.APPROVED
            )
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=ToolRegistry(
                LocalReadToolExecutor(),
                patch_executor=LocalPatchToolExecutor(),
                capabilities=LOCAL_CAPABILITIES,
            ),
        )
        try:
            if saved_decision:
                await service.resume_decided_approvals()
                await service.wait_for_run(run.id)
                finished = await database.get_run(run.id)
                assert finished is not None and finished.status is RunStatus.FAILED
                assert finished.error_code == "capability_disabled"
            else:
                with pytest.raises(ApplicationError) as error:
                    await service.respond_to_tool_approval(
                        run.id, calls[0].id, ToolApprovalDecision.APPROVED
                    )
                assert error.value.code == "capability_disabled"
                assert (await database.list_tool_calls(run.id))[0].approval_decision is None
        finally:
            await service.close()

    asyncio.run(exercise())
    assert provider.requests == []
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "before"


def test_workspace_independent_tool_round_trip_needs_no_local_permission(tmp_path: Path) -> None:
    from app.application.tools import ToolRegistration

    class LookupArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        record: str

    class LookupExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path | None
        ) -> tuple[str, bool]:
            assert name == "lookup_record" and workspace_root is None
            return f"Remote record: {arguments['record']}", False

    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("lookup", "lookup_record", {"record": "entry-7"}),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="Found entry-7."),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )
    registry = ToolRegistry(
        additional_tools=(
            ToolRegistration(
                "lookup_record", "Look up a remote record", LookupArguments, LookupExecutor()
            ),
        )
    )

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", "sk-test-key")
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=registry,
        )
        try:
            run = await service.create_run(session.id, "lookup-request", "Find entry-7")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert [tool.name for tool in provider.requests[0].tools] == ["lookup_record"]
            assert provider.requests[1].messages[-1].content == "Remote record: entry-7"
            assert provider.requests[1].messages[-1].tool_call_id == "lookup"
        finally:
            await service.close()

    asyncio.run(exercise())
