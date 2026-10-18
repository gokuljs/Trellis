import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from app.application.execution import RunExecutor
from app.application.worker_ports import WorkerExecutionRepository
from app.domain.jobs import RunExecutionContext
from app.domain.runtime import ModelStreamEvent, RunEventType, RunStatus
from app.infrastructure.server_credentials import ServerCredentialStore
from tests.memory_repository import MemoryRepository
from tests.support import make_settings
from tests.test_runtime_service import RecordingProvider


def test_executor_requires_confirmed_begin_before_any_provider_access(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = MemoryRepository(tmp_path)
        session = await repository.create_session()
        model = (await repository.list_models())[0]
        run = await repository.create_run(session.id, "turn", "request", "Hello", model)
        executor = RunExecutor(
            cast(WorkerExecutionRepository, repository),
            ServerCredentialStore(make_settings(tmp_path)),
            {},
        )
        with pytest.raises(ValueError, match="begin before provider"):
            await executor.execute(RunExecutionContext("owner", run, session, model))
        saved = await repository.get_run(run.id)
        assert saved is not None and saved.status is RunStatus.QUEUED

    asyncio.run(scenario())


def test_executor_uses_only_assigned_context_and_preserves_streaming(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = MemoryRepository(tmp_path)
        session = await repository.create_session()
        model = (await repository.list_models())[0]
        run = await repository.create_run(session.id, "turn", "request", "Hello", model)
        await repository.transition_run_record(
            run.id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
        )
        run = await repository.get_run(run.id)
        assert run is not None
        provider = RecordingProvider(
            (
                ModelStreamEvent(kind="text_delta", text="Saved answer"),
                ModelStreamEvent(kind="completed"),
            )
        )
        executor = RunExecutor(
            cast(WorkerExecutionRepository, repository),
            ServerCredentialStore(make_settings(tmp_path, openai_api_key="server-key")),
            {model.adapter_kind: provider},
        )
        context = RunExecutionContext("assigned-owner", run, session, model)
        with pytest.raises(ValueError, match="does not match"):
            await executor.execute(replace(context, session=replace(session, id="foreign")))
        with pytest.raises(ValueError, match="does not match"):
            await executor.execute(
                replace(context, model=replace(model, upstream_model_id="foreign"))
            )
        assert provider.request is None
        await executor.execute(context)
        saved = await repository.get_run(run.id)
        assert saved is not None and saved.status is RunStatus.COMPLETED
        assert provider.user_id == "assigned-owner"
        assert provider.request is not None and provider.request.tools == ()
        assert (await repository.list_messages(session.id))[-1].content == "Saved answer"

    asyncio.run(scenario())
