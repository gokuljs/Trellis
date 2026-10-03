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
    usage = ModelStreamEvent(kind="usage", input_tokens=8, output_tokens=3)
    completed = ModelStreamEvent(kind="completed", finish_reason="stop")

    assert delta.text == "hello"
    assert (usage.input_tokens, usage.output_tokens) == (8, 3)
    assert completed.finish_reason == "stop"
