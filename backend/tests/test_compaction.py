import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace

import httpx
import pytest

from app.application.compaction import build_summary_request, replay_checkpoint, summarize_prefix
from app.application.errors import ApplicationError, ProviderError
from app.application.secret_redaction import SecretRedactor
from app.domain.context import ContextItem
from app.domain.runtime import (
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ModelToolSpec,
)
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.test_context_checkpoints import checkpoint_row
from tests.test_supabase_repository import ALICE, SETTINGS, verified_token_source


def request() -> ModelRequest:
    return ModelRequest(
        "openai",
        "openai:gpt-5.5",
        "openai",
        "gpt-5.5",
        (),
        4096,
        "You are a helpful assistant.",
        (ModelToolSpec("read_file", "Read", {"type": "object"}),),
    )


class SummaryProvider:
    def __init__(
        self, events: tuple[ModelStreamEvent, ...], *, failure: bool = False, wait: bool = False
    ) -> None:
        self.events = events
        self.failure = failure
        self.wait = wait
        self.closed = False

    async def stream(
        self, _request: ModelRequest, _api_key: str, _user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        try:
            for event in self.events:
                yield event
            if self.failure:
                raise ProviderError("provider_upstream_failed", "Provider failed")
            if self.wait:
                await asyncio.Event().wait()
        finally:
            self.closed = True


def test_summary_request_uses_only_eligible_prefix_with_private_bounded_no_tools_prompt() -> None:
    prefix = (ContextItem("first", "old", ModelMessage("user", "Keep my preference.")),)
    built = build_summary_request(request(), prefix)
    assert built.max_output_tokens == 2048 and built.tools == ()
    assert built.provider_id == request().provider_id and built.model_id == request().model_id
    assert len(built.messages) == 1 and built.messages[0].role == "user"
    contents = json.loads(built.messages[0].content)
    assert contents["history"][0]["content"] == "Keep my preference."
    assert "historical" in built.system_instructions and "tools" in built.system_instructions
    assert "You are a helpful assistant." not in built.system_instructions


@pytest.mark.parametrize(
    "prefix", [(), (ContextItem("now", "now", ModelMessage("user", "Current"), required=True),)]
)
def test_summary_request_rejects_empty_or_current_request_prefix(
    prefix: tuple[ContextItem, ...],
) -> None:
    with pytest.raises(ApplicationError):
        build_summary_request(request(), prefix)


def test_summary_is_private_until_complete_preserves_whitespace_and_redacts_finished_text() -> None:
    events = (
        ModelStreamEvent("text_delta", text="  Keep "),
        ModelStreamEvent("text_delta", text="secret-value.\n"),
        ModelStreamEvent("usage", input_tokens=40, output_tokens=12),
        ModelStreamEvent("completed", finish_kind="final", provider_response_id="summary-id"),
    )
    provider = SummaryProvider(events)
    usage: list[ModelStreamEvent] = []

    async def record(event: ModelStreamEvent) -> None:
        usage.append(event)

    result = asyncio.run(
        summarize_prefix(
            provider,
            request(),
            "key",
            "user",
            timeout_seconds=1,
            redactor=SecretRedactor("secret-value"),
            on_usage=record,
        )
    )
    assert result.text == "  Keep [REDACTED].\n" and result.provider_response_id == "summary-id"
    assert usage == [events[2]] and provider.closed


@pytest.mark.parametrize("terminal", [None, "tool", "incomplete"])
def test_partial_or_missing_summary_completion_is_rejected(terminal: str | None) -> None:
    events = [ModelStreamEvent("text_delta", text="Partial")]
    if terminal is not None:
        assert terminal in {"tool", "incomplete"}
        events.append(ModelStreamEvent("completed", finish_kind=terminal))
    provider = SummaryProvider(tuple(events))
    with pytest.raises(ApplicationError, match="complete"):
        asyncio.run(summarize_prefix(provider, request(), "key", "user", timeout_seconds=1))
    assert provider.closed


@pytest.mark.parametrize("text", ["", "  \n", "é" * 16385], ids=["empty", "blank", "oversized"])
def test_empty_and_oversized_summary_cannot_be_installed(text: str) -> None:
    provider = SummaryProvider(
        (
            ModelStreamEvent("text_delta", text=text),
            ModelStreamEvent("completed", finish_kind="final"),
        )
    )
    with pytest.raises(ApplicationError):
        asyncio.run(summarize_prefix(provider, request(), "key", "user", timeout_seconds=1))
    assert provider.closed


def test_summary_tool_request_is_rejected_without_execution() -> None:
    provider = SummaryProvider(
        (ModelStreamEvent("tool_call", tool_call=ModelToolCall("call", "read_file", {})),)
    )
    with pytest.raises(ApplicationError, match="tool"):
        asyncio.run(summarize_prefix(provider, request(), "key", "user", timeout_seconds=1))
    assert provider.closed


def test_summary_usage_is_recorded_before_later_provider_failure() -> None:
    event = ModelStreamEvent("usage", input_tokens=90, output_tokens=3)
    provider = SummaryProvider(
        (event, ModelStreamEvent("text_delta", text="Partial")), failure=True
    )
    recorded: list[ModelStreamEvent] = []

    async def record(item: ModelStreamEvent) -> None:
        recorded.append(item)

    with pytest.raises(ProviderError):
        asyncio.run(
            summarize_prefix(provider, request(), "key", "user", timeout_seconds=1, on_usage=record)
        )
    assert recorded == [event] and provider.closed


def test_summary_timeout_and_cancellation_close_the_provider_stream() -> None:
    async def exercise() -> None:
        provider = SummaryProvider((), wait=True)
        with pytest.raises(ApplicationError, match="timed out"):
            await summarize_prefix(provider, request(), "key", "user", timeout_seconds=0.001)
        assert provider.closed
        provider = SummaryProvider((), wait=True)
        task = asyncio.create_task(
            summarize_prefix(provider, request(), "key", "user", timeout_seconds=10)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed

    asyncio.run(exercise())


def test_checkpoint_replay_uses_native_only_for_identical_model_and_fingerprints() -> None:
    async def exercise() -> None:
        row = json.loads(json.dumps(checkpoint_row()))
        row["native_kind"] = "openai_window"
        row["native_payload_json"] = (
            '[ {"type":"compaction","encrypted_content":"opaque"}, '
            '{"role":"user","content":"retained"} ]'
        )
        async with httpx.AsyncClient() as client:
            checkpoint = SupabaseRepository(
                SETTINGS, ALICE, verified_token_source(ALICE), client
            )._checkpoint(row)
        native = replay_checkpoint(
            checkpoint,
            request(),
            instruction_hash="c" * 64,
            workspace_hash="d" * 64,
            allow_native=True,
        )
        assert native.native_payload_json == row["native_payload_json"] and native.message is None
        switched = replay_checkpoint(
            checkpoint,
            replace(request(), model_id="openai:gpt-5.4"),
            instruction_hash="c" * 64,
            workspace_hash="d" * 64,
            allow_native=True,
        )
        assert switched.native_payload_json is None and switched.message is not None
        assert (
            switched.message.role == "user" and checkpoint.draft.summary in switched.message.content
        )
        for instruction, workspace in [("x" * 64, "d" * 64), ("c" * 64, "x" * 64)]:
            replay = replay_checkpoint(
                checkpoint,
                request(),
                instruction_hash=instruction,
                workspace_hash=workspace,
                allow_native=True,
            )
            assert replay.message is not None and replay.native_payload_json is None

    asyncio.run(exercise())


def test_summary_rejects_events_after_final_completion() -> None:
    provider = SummaryProvider(
        (
            ModelStreamEvent("text_delta", text="Summary"),
            ModelStreamEvent("completed", finish_kind="final"),
            ModelStreamEvent("text_delta", text="extra"),
        )
    )
    with pytest.raises(ApplicationError, match="cleanly"):
        asyncio.run(summarize_prefix(provider, request(), "key", "user", timeout_seconds=1))
    assert provider.closed


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_summary_rejects_invalid_deadlines(timeout: float) -> None:
    provider = SummaryProvider(())
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(summarize_prefix(provider, request(), "key", "user", timeout_seconds=timeout))
