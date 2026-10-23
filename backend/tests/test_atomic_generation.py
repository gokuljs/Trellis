import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from run_fixtures import Provider, Repository, plan, service

from app.application.run_context import RunContext
from app.domain.runtime import (
    ModelCallStatus,
    ModelMessage,
    ModelStreamEvent,
    ModelToolCall,
    ModelToolSpec,
    RunEventType,
    RunStatus,
)


class AtomicRepository(Repository):
    def __init__(self) -> None:
        super().__init__()
        self.boundaries: list[
            tuple[str, str, dict[str, object], ModelMessage | None, str | None]
        ] = []

    async def commit_generation(
        self, run_id, call_id, completion, *, assistant_message=None, final_content=None
    ):
        assert self.calls[-1].status is ModelCallStatus.STREAMING
        assert self.run.status is RunStatus.RUNNING
        self.boundaries.append((run_id, call_id, completion, assistant_message, final_content))
        return await super().commit_generation(
            run_id,
            call_id,
            completion,
            assistant_message=assistant_message,
            final_content=final_content,
        )

    async def complete_run(self, *args, **kwargs):
        raise AssertionError("A completed generation must publish its answer atomically.")

    async def record_assistant_message(self, *args, **kwargs):
        raise AssertionError("A completed generation must publish its tools atomically.")

    async def finish_assistant_output(self, *args, **kwargs):
        raise AssertionError("A completed generation must finalize its output atomically.")


@pytest.mark.parametrize("invalid", [False, True])
def test_final_generation_commits_usage_and_publication_together(monkeypatch, invalid) -> None:
    repo = AtomicRepository()
    provider = Provider()
    if invalid:

        async def incomplete(*args):
            yield ModelStreamEvent("text_delta", text="partial")
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="incomplete")

        monkeypatch.setattr(provider, "stream", incomplete)
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))

    async def exercise() -> None:
        runtime = service(repo, provider)
        admitted = await runtime.create_run("session", "request", "test input")
        await runtime.wait_for_run(admitted.id)
        await runtime.close()

    asyncio.run(exercise())
    call = repo.calls[-1]
    assert call.request_snapshot["purpose"] == "generation"
    assert call.status is ModelCallStatus.COMPLETED
    assert call.accounted_total_tokens == 110
    assert call.response_snapshot is not None
    assert call.response_snapshot["finish_kind"] == ("incomplete" if invalid else "final")
    if invalid:
        assert not repo.boundaries
        assert repo.run.status is RunStatus.FAILED
        assert not any(event.event_type is RunEventType.COMPLETED for event in repo.events)
    else:
        assert len(repo.boundaries) == 1
        _run_id, _call_id, completion, assistant, final = repo.boundaries[0]
        assert (assistant, final) == (None, "answer")
        assert completion["input_tokens"] == 100
        assert repo.run.status is RunStatus.COMPLETED
        assert [event.event_type for event in repo.events][-2:] == [
            RunEventType.MODEL_COMPLETED,
            RunEventType.COMPLETED,
        ]


@pytest.mark.parametrize("invalid", [None, "unoffered", "repeated", "limit"])
def test_tool_exchange_is_validated_before_atomic_publication(monkeypatch, invalid) -> None:
    repo = AtomicRepository()
    if invalid == "limit":
        repo.run = replace(repo.run, max_model_calls=1)
    requested = ModelToolCall("tool-1", "other" if invalid == "unoffered" else "read_file", {})

    class ToolProvider(Provider):
        async def stream(self, *args):
            yield ModelStreamEvent("text_delta", text="I will inspect the file.")
            yield ModelStreamEvent("tool_call", tool_call=requested)
            if invalid == "repeated":
                yield ModelStreamEvent("tool_call", tool_call=requested)
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="tool")

    prepared = plan()
    prepared = replace(
        prepared,
        request=replace(prepared.request, tools=(ModelToolSpec("read_file", "Read a file", {}),)),
    )
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=prepared))

    async def exercise() -> None:
        runtime = service(repo, ToolProvider())
        runtime._tool_registry = Mock()
        runtime._tool_registry.specs.return_value = prepared.request.tools
        runtime._execute_pending_tools = AsyncMock(return_value=False)
        admitted = await runtime.create_run("session", "request", "test input")
        await runtime.wait_for_run(admitted.id)
        if invalid is None:
            runtime._execute_pending_tools.assert_awaited_once()
            assert len(repo.boundaries) == 1
            _run_id, _call_id, completion, assistant, final = repo.boundaries[0]
            assert assistant is not None
            assert assistant.tool_calls == (requested,)
            assert assistant.content == "I will inspect the file."
            assert final is None
            assert completion["accounted_total_tokens"] == 110
        else:
            runtime._execute_pending_tools.assert_not_awaited()
            assert repo.boundaries == []
            assert repo.run.status is RunStatus.FAILED
        await runtime.close()

    asyncio.run(exercise())
    assert repo.calls[-1].status is ModelCallStatus.COMPLETED
    assert repo.calls[-1].accounted_total_tokens == 110
