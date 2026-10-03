import pytest

from app.domain.runtime import ModelStreamEvent, RunStatus, transition_run


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
