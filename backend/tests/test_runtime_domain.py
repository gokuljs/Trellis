import pytest

from app.domain.runtime import (
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ModelToolSpec,
    RunStatus,
    transition_run,
)


def test_run_state_transitions_allow_only_lifecycle_progression() -> None:
    assert transition_run(RunStatus.QUEUED, RunStatus.RUNNING) is RunStatus.RUNNING
    assert transition_run(RunStatus.RUNNING, RunStatus.COMPLETED) is RunStatus.COMPLETED
    assert transition_run(RunStatus.QUEUED, RunStatus.CANCELLED) is RunStatus.CANCELLED
    assert transition_run(RunStatus.RUNNING, RunStatus.INTERRUPTED) is RunStatus.INTERRUPTED


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.COMPLETED),
        (RunStatus.CANCELLED, RunStatus.QUEUED),
        (RunStatus.RUNNING, RunStatus.QUEUED),
    ],
)
def test_run_state_transitions_reject_regression_and_terminal_rewrites(
    current: RunStatus,
    next_status: RunStatus,
) -> None:
    with pytest.raises(ValueError, match="invalid run transition"):
        transition_run(current, next_status)


def test_normalized_model_stream_events_do_not_depend_on_provider_sdk_types() -> None:
    delta = ModelStreamEvent(kind="text_delta", text="hello")
    usage = ModelStreamEvent(
        kind="usage",
        input_tokens=8,
        output_tokens=3,
        reasoning_tokens=1,
        cached_tokens=2,
        provider_response_id="response-1",
    )
    completed = ModelStreamEvent(kind="completed", finish_reason="stop")

    assert delta.text == "hello"
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.cached_tokens,
    ) == (8, 3, 1, 2)
    assert usage.provider_response_id == "response-1"
    assert completed.finish_reason == "stop"


@pytest.mark.parametrize("field", ["reasoning_tokens", "cached_tokens"])
def test_normalized_model_usage_rejects_negative_optional_counts(field: str) -> None:
    with pytest.raises(ValueError, match="token counts cannot be negative"):
        if field == "reasoning_tokens":
            ModelStreamEvent(kind="usage", reasoning_tokens=-1)
        else:
            ModelStreamEvent(kind="usage", cached_tokens=-1)


def test_model_request_carries_tools_and_their_round_trip_messages() -> None:
    call = ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})
    tool = ModelToolSpec(
        name="read_file",
        description="Read a workspace file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    request = ModelRequest(
        provider_id="openai",
        model_id="openai:test",
        adapter_kind="openai",
        upstream_model_id="test",
        messages=(
            ModelMessage(role="assistant", content="I'll read it.", tool_calls=(call,)),
            ModelMessage(role="tool", content="File contents", tool_call_id="call-1"),
        ),
        max_output_tokens=256,
        system_instructions="Follow the workspace rules.",
        tools=(tool,),
    )

    assert request.messages[0].tool_calls == (call,)
    assert request.messages[1].tool_call_id == "call-1"
    assert request.tools == (tool,)
    assert request.system_instructions == "Follow the workspace rules."
    assert ModelStreamEvent(kind="tool_call", tool_call=call).tool_call == call


def test_stream_rejects_tool_call_event_without_a_complete_call() -> None:
    with pytest.raises(ValueError, match="tool_call events require a tool call"):
        ModelStreamEvent(kind="tool_call")
