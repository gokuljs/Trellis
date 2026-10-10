import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from app.application.runs import RunService
from app.application.tools import ToolRegistry
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunSnapshot,
    RunStatus,
)
from app.infrastructure.database import Database
from app.infrastructure.secrets import SecretStore


class LeaseDatabase(Database):
    def __init__(self, path: Path, *, owns_created_run: bool = True, lose_after: int = 0) -> None:
        super().__init__(path)
        self.owns_created_run = owns_created_run
        self.lose_after = lose_after
        self.owned: set[str] = set()
        self.renewals = 0
        self.renewed_twice = asyncio.Event()
        self.allow_loss = asyncio.Event()

    async def create_run(self, *args, **kwargs) -> RunSnapshot:  # type: ignore[override]
        run = await super().create_run(*args, **kwargs)
        if self.owns_created_run:
            self.owned.add(run.id)
        return run

    def owns_run_lease(self, run_id: str) -> bool:
        return run_id in self.owned

    async def renew_run_lease(self, run_id: str) -> RunSnapshot:
        self.renewals += 1
        if self.renewals >= 2:
            self.renewed_twice.set()
        if self.lose_after and self.allow_loss.is_set() and self.renewals >= self.lose_after:
            self.owned.discard(run_id)
            raise ValueError("lease lost")
        run = await self.get_run(run_id)
        assert run is not None
        return run


class BlockingProvider:
    name = "openai"
    model = "gpt-5.5"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        del request, api_key, user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()
            yield ModelStreamEvent(kind="completed")

        return generate()

    async def complete(self, *_args: object, **_kwargs: object) -> str:
        raise NotImplementedError


def test_locally_owned_run_renews_its_lease_while_streaming(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = LeaseDatabase(tmp_path / "state.db")
        await database.initialize()
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        provider = BlockingProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            lease_repository=database,
            lease_renew_interval_seconds=0.01,
        )
        try:
            created = await service.create_run(session.id, "request", "Hello")
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            await asyncio.wait_for(database.renewed_twice.wait(), timeout=1)
            assert database.owns_run_lease(created.id)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_foreign_queued_run_is_never_scheduled_locally(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = LeaseDatabase(tmp_path / "state.db", owns_created_run=False)
        await database.initialize()
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        provider = BlockingProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            lease_repository=database,
            lease_renew_interval_seconds=0.01,
        )
        try:
            created = await service.create_run(session.id, "request", "Hello")
            await asyncio.sleep(0.05)
            assert created.status is RunStatus.QUEUED
            assert not provider.started.is_set()
            assert database.renewals == 0
        finally:
            await service.close()

    asyncio.run(exercise())


def test_lost_lease_stops_the_local_provider_without_writing_more(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = LeaseDatabase(tmp_path / "state.db", lose_after=1)
        await database.initialize()
        session = await database.create_session()
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        provider = BlockingProvider()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            lease_repository=database,
            lease_renew_interval_seconds=0.01,
        )
        try:
            created = await service.create_run(session.id, "request", "Hello")
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            database.allow_loss.set()
            await asyncio.wait_for(provider.stopped.wait(), timeout=1)
            assert not database.owns_run_lease(created.id)
            run = await database.get_run(created.id)
            assert run is not None and run.status is RunStatus.RUNNING
        finally:
            await service.close()

    asyncio.run(exercise())


def test_lost_lease_prevents_local_tool_execution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class ToolProvider(BlockingProvider):
        def __init__(self, allow_loss: asyncio.Event) -> None:
            super().__init__()
            self.allow_loss = allow_loss

        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            del request, api_key, user_id

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                self.allow_loss.set()
                yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-call", "read_file", {"path": "note.txt"}),
                )
                yield ModelStreamEvent(kind="completed", finish_reason="tool_use")

            return generate()

    class SpyReadExecutor:
        def __init__(self) -> None:
            self.called = False

        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            del name, arguments, workspace_root
            self.called = True
            return "unexpected", False

    async def exercise() -> None:
        database = LeaseDatabase(tmp_path / "state.db", lose_after=1)
        await database.initialize()
        session = await database.create_session(str(project))
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "sk-test")
        provider = ToolProvider(database.allow_loss)
        executor = SpyReadExecutor()
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            tool_registry=ToolRegistry(executor),
            lease_repository=database,
            lease_renew_interval_seconds=60,
        )
        try:
            created = await service.create_run(session.id, "request", "Read note")
            await service.wait_for_run(created.id)
            assert not executor.called
            assert not database.owns_run_lease(created.id)
        finally:
            await service.close()

    asyncio.run(exercise())
