import asyncio
from pathlib import Path

from app.application.runs import RunService
from app.domain.models import UserProfile
from app.domain.runtime import RunStatus
from app.infrastructure.server_credentials import ServerCredentialStore
from tests.memory_repository import MemoryRepository
from tests.support import make_settings
from tests.test_runtime_api import StreamingProvider


def test_legacy_shutdown_interrupts_after_begin_before_context_loading(tmp_path: Path) -> None:
    class BlockingProfileRepository(MemoryRepository):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.loading = asyncio.Event()

        async def get_profile(self) -> UserProfile:
            self.loading.set()
            await asyncio.Event().wait()
            return await super().get_profile()

    async def scenario():
        database = BlockingProfileRepository(tmp_path)
        session = await database.create_session()
        service = RunService(
            database,
            database,
            database,
            database,
            ServerCredentialStore(make_settings(tmp_path, openai_api_key="key")),
            {"openai": StreamingProvider()},
        )
        run = await service.create_run(session.id, "request", "Hello")
        await asyncio.wait_for(database.loading.wait(), 1)
        await service.close()
        saved = await database.get_run(run.id)
        assert saved is not None and saved.status is RunStatus.INTERRUPTED

    asyncio.run(scenario())
