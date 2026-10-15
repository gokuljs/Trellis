import asyncio
from pathlib import Path

import pytest

from app.application.errors import ProviderError
from app.application.runs import RunService
from app.domain.runtime import ModelRequest, ModelStreamEvent, RunEventType, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.support import make_settings


@pytest.mark.parametrize("outcome", ["complete", "failure", "cancel", "incomplete"])
def test_published_output_survives_each_terminal_outcome(tmp_path: Path, outcome: str) -> None:
    async def exercise():
        settings = make_settings(tmp_path)
        database = MemoryRepository(settings.data_dir / "memory")
        secrets = SecretStore(settings.data_dir / ".env")
        release = asyncio.Event()
        closed = asyncio.Event()
        published = asyncio.Event()

        class Publisher:
            async def publish(self, event):
                stored = await database.list_run_events(event.run_id, event.sequence - 1, limit=1)
                assert stored == [event]
                if event.event_type is RunEventType.ASSISTANT_DELTA:
                    published.set()

        class Provider:
            name = "openai"
            model = "test-model"

            def stream(self, request: ModelRequest, api_key: str, user_id: str):
                async def events():
                    try:
                        yield ModelStreamEvent(kind="text_delta", text=" visible answer \n")
                        await release.wait()
                        if outcome == "failure":
                            raise ProviderError("provider_upstream_failed", "Provider failed.")
                        yield ModelStreamEvent(kind="text_delta", text="tail ")
                        yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=4)
                        yield ModelStreamEvent(
                            kind="completed",
                            finish_kind="incomplete" if outcome == "incomplete" else "final",
                        )
                    finally:
                        closed.set()

                return events()

            async def complete(self, *_args, **_kwargs):
                raise NotImplementedError

        await secrets.set("openai", "test-key")
        session = await database.create_session()
        service = RunService(
            database, database, database, database, secrets, {"openai": Provider()}, Publisher()
        )
        run = await service.create_run(session.id, "stream", "Hello")
        try:
            async with asyncio.timeout(1):
                await published.wait()
            live = (await database.list_assistant_outputs(session.id))[0]
            assert live.content == " visible answer \n" and live.status == "streaming"
            assert not closed.is_set(), "the provider must still be generating"
            if outcome == "cancel":
                await service.cancel_run(run.id)
                with pytest.raises(asyncio.CancelledError):
                    await service.wait_for_run(run.id)
            else:
                release.set()
                await service.wait_for_run(run.id)
            assert closed.is_set()
            saved = (
                await MemoryRepository(settings.data_dir / "memory").list_assistant_outputs(
                    session.id
                )
            )[0]
            messages = await database.list_messages(session.id)
            events = await database.list_run_events(run.id)
            shown = "".join(
                str(e.data["text"]) for e in events if e.event_type is RunEventType.ASSISTANT_DELTA
            )
            assert shown == saved.content
            final_run = await database.get_run(run.id)
            assert final_run is not None
            if outcome == "complete":
                assert final_run.status is RunStatus.COMPLETED
                assert saved.status == "complete" and saved.content == messages[-1].content
                final_event = next(
                    e for e in events if e.event_type is RunEventType.ASSISTANT_COMPLETED
                )
                assert final_event.data["output_id"] == saved.id
            else:
                assert saved.status == "partial" and len(messages) == 1
        finally:
            release.set()
            await service.close()

    asyncio.run(exercise())
