import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from app.application.runs import RunService
from app.application.sessions import SessionService
from app.application.tools import ToolRegistry
from app.domain.runtime import ModelRequest, ModelStreamEvent, RunStatus
from app.infrastructure.database import Database
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
from app.infrastructure.secrets import SecretStore
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings


def test_cloud_workspace_path_requires_a_local_selection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = Database(tmp_path / "state.db")

    async def exercise() -> None:
        await database.initialize()
        session = await database.create_session(str(project))
        local_bindings = LocalWorkspaceBindings()
        other_computer = LocalWorkspaceBindings()
        service = SessionService(database, workspace_bindings=local_bindings)

        assert await local_bindings.resolve(session.id, session.workspace_path) is None
        await service.set_workspace(session.id, str(project))
        assert await local_bindings.resolve(session.id, session.workspace_path) == project
        assert await other_computer.resolve(session.id, session.workspace_path) is None
        assert await local_bindings.resolve(session.id, str(tmp_path)) is None

        await service.set_workspace(session.id, None)
        assert await local_bindings.resolve(session.id, session.workspace_path) is None

    asyncio.run(exercise())


def test_cloud_workspace_metadata_cannot_offer_local_tools(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Private local guidance.\n", encoding="utf-8")
    database = Database(tmp_path / "state.db")
    secret_store = SecretStore(tmp_path / ".env")

    class Provider:
        name = "openai"
        model = "gpt-5.5"

        def __init__(self) -> None:
            self.request: ModelRequest | None = None

        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            del api_key, user_id
            self.request = request

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                yield ModelStreamEvent(kind="text_delta", text="Done")
                yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(kind="completed", finish_reason="stop")

            return generate()

        async def complete(self, *_args: object, **_kwargs: object) -> str:
            raise NotImplementedError

    async def exercise() -> None:
        await database.initialize()
        cloud_chat = await database.create_session(str(project))
        await secret_store.set("openai", "sk-local-test")
        provider = Provider()
        service = RunService(
            database,
            database,
            database,
            database,
            secret_store,
            {"openai": provider},
            tool_registry=ToolRegistry(LocalReadToolExecutor()),
            workspace_guidance_reader=read_workspace_guidance,
            workspace_bindings=LocalWorkspaceBindings(),
        )
        try:
            created = await service.create_run(cloud_chat.id, "request-1", "Hello")
            await service.wait_for_run(created.id)
            finished = await database.get_run(created.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert provider.request is not None
            assert provider.request.tools == ()
            assert "Private local guidance" not in provider.request.system_instructions
        finally:
            await service.close()

    asyncio.run(exercise())


def test_replaced_workspace_path_invalidates_local_binding(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    async def exercise() -> None:
        bindings = LocalWorkspaceBindings()
        bindings.bind("chat", project)
        assert await bindings.resolve("chat", str(project)) == project

        project.rmdir()
        project.symlink_to(replacement, target_is_directory=True)
        assert await bindings.resolve("chat", str(project)) is None

    asyncio.run(exercise())
