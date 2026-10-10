import asyncio
from pathlib import Path

import pytest

from app.application.context import build_model_context
from app.domain.runtime import (
    ModelCallStatus,
    ModelContinuationItem,
    ModelMessage,
    ModelToolCall,
    RunEventType,
    RunStatus,
)
from tests.memory_repository import MemoryRepository


def test_internal_tool_transcript_replays_in_order_without_polluting_public_chat(
    tmp_path: Path,
) -> None:
    database = MemoryRepository(tmp_path / "cloud-account")

    async def record() -> tuple[str, str]:
        await database.initialize()
        session = await database.create_session()
        model = (await database.list_models())[0]
        run = await database.create_run(session.id, "turn-1", "request-1", "Inspect files", model)
        await database.transition_run_record(
            run.id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
        )
        model_call = await database.create_model_call(run.id, 1, model, {"messages": []})
        await database.update_model_call(
            model_call.id,
            ModelCallStatus.COMPLETED,
            response_snapshot={"text": "Checking", "tool_calls": ["provider-a", "provider-b"]},
        )
        assistant, calls, _events = await database.record_assistant_message(
            run.id,
            model_call.id,
            ModelMessage(
                role="assistant",
                content="Checking both files.",
                continuation_items=(
                    ModelContinuationItem(
                        "openai", model.id, '{"type":"reasoning","encrypted_content":"opaque"}'
                    ),
                ),
                tool_calls=(
                    ModelToolCall("provider-a", "read_file", {"path": "a.py"}),
                    ModelToolCall("provider-b", "read_file", {"path": "b.py"}),
                ),
            ),
        )
        assert assistant.ordinal == 1
        assert [call.provider_call_id for call in calls] == ["provider-a", "provider-b"]
        await database.record_tool_result(run.id, calls[1].id, "contents of b")
        await database.record_tool_result(run.id, calls[0].id, "contents of a")
        with pytest.raises(ValueError, match="already has a result"):
            await database.record_tool_result(run.id, calls[0].id, "duplicate")
        second_model_call = await database.create_model_call(run.id, 2, model, {"messages": []})
        await database.update_model_call(second_model_call.id, ModelCallStatus.COMPLETED)
        await database.record_assistant_message(
            run.id,
            second_model_call.id,
            ModelMessage(role="assistant", content="Both files are ready."),
        )
        await database.complete_run(run.id, "Both files are ready.")
        return run.id, session.id

    run_id, session_id = asyncio.run(record())
    restarted = MemoryRepository(tmp_path / "cloud-account")
    agent_messages = asyncio.run(restarted.list_run_messages(run_id))
    tool_calls = asyncio.run(restarted.list_tool_calls(run_id))
    public_messages = asyncio.run(restarted.list_messages(session_id))
    events = asyncio.run(restarted.list_run_events(run_id))
    session = asyncio.run(restarted.get_session(session_id))

    assert [(message.ordinal, message.role, message.content) for message in agent_messages] == [
        (1, "assistant", "Checking both files."),
        (2, "tool", "contents of b"),
        (3, "tool", "contents of a"),
        (4, "assistant", "Both files are ready."),
    ]
    assert [message.tool_call_id for message in agent_messages[1:3]] == [
        tool_calls[1].id,
        tool_calls[0].id,
    ]
    replay = build_model_context(
        public_messages[:1],
        agent_messages[:3],
        workspace_root=None,
        run_tool_calls=tool_calls,
        tools=[],
    )
    assert [message.tool_call_id for message in replay.messages[2:]] == [
        "provider-b",
        "provider-a",
    ]
    assert agent_messages[0].continuation_items == (
        ModelContinuationItem(
            "openai", "openai:gpt-5.5", '{"type":"reasoning","encrypted_content":"opaque"}'
        ),
    )
    assert [call.name for call in tool_calls] == ["read_file", "read_file"]
    assert [call.arguments for call in tool_calls] == [
        {"path": "a.py"},
        {"path": "b.py"},
    ]
    assert [call.status for call in tool_calls] == ["completed", "completed"]
    assert [(message.role, message.content) for message in public_messages] == [
        ("user", "Inspect files"),
        ("assistant", "Both files are ready."),
    ]
    assert session is not None and session.message_count == 2
    assert [(event.sequence, event.event_type.value) for event in events] == [
        (1, "run.queued"),
        (2, "run.started"),
        (3, "assistant.message"),
        (4, "tool.call"),
        (5, "tool.call"),
        (6, "tool.result"),
        (7, "tool.result"),
        (8, "assistant.message"),
        (9, "assistant.completed"),
        (10, "run.completed"),
    ]
