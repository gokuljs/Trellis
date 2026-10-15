import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.application.runs import RunService
from app.domain.runtime import ModelMessage, ModelRequest, RunEventType, RunStatus
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.support import make_settings


def _terminal_stream(provider: str, reason: str) -> bytes:
    if provider == "openai":
        terminal_type = "response.completed" if reason == "completed" else "response.incomplete"
        events = [
            {"type": "response.output_text.delta", "delta": "The answer starts here."},
            {
                "type": terminal_type,
                "response": {
                    "id": "response-finish",
                    "incomplete_details": {"reason": reason},
                    "usage": {"input_tokens": 2, "output_tokens": 5},
                },
            },
        ]
    else:
        events = [
            {
                "type": "message_start",
                "message": {"id": "response-finish", "usage": {"input_tokens": 2}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "The answer starts here."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": reason},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


@pytest.mark.parametrize(
    ("provider", "reason", "expected_finish"),
    [
        ("openai", "completed", "final"),
        ("openai", "max_output_tokens", "incomplete"),
        ("openai", "content_filter", "incomplete"),
        ("anthropic", "end_turn", "final"),
        ("anthropic", "stop_sequence", "final"),
        ("anthropic", "refusal", "final"),
        ("anthropic", "max_tokens", "incomplete"),
        ("anthropic", "model_context_window_exceeded", "incomplete"),
        ("anthropic", "pause_turn", "incomplete"),
        ("anthropic", "future_stop", "incomplete"),
    ],
)
def test_provider_normalizes_terminal_response_kind(
    provider: str, reason: str, expected_finish: str
) -> None:
    async def run():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_terminal_stream(provider, reason))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIProvider(client) if provider == "openai" else AnthropicProvider(client)
            request = ModelRequest(
                provider_id=provider,
                model_id=f"{provider}:test",
                adapter_kind=provider,
                upstream_model_id="test",
                messages=(ModelMessage(role="user", content="Answer"),),
                max_output_tokens=256,
            )
            return [event async for event in adapter.stream(request, "test-key", "test-user")]

    events = asyncio.run(run())
    assert events[-1].kind == "completed"
    assert events[-1].finish_kind == expected_finish
    assert events[-1].finish_reason == reason


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        ("openai", "max_output_tokens"),
        ("anthropic", "max_tokens"),
        ("anthropic", "model_context_window_exceeded"),
        ("anthropic", "pause_turn"),
    ],
)
def test_provider_incomplete_text_does_not_complete_a_run(
    tmp_path: Path, provider: str, reason: str
) -> None:
    settings = make_settings(tmp_path)
    database = MemoryRepository(settings.data_dir / "memory")
    secret_store = SecretStore(settings.data_dir / ".env")

    async def run():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_terminal_stream(provider, reason))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenAIProvider(client) if provider == "openai" else AnthropicProvider(client)
            session = await database.create_session()
            await secret_store.set("openai", "test-key")
            service = RunService(
                database, database, database, database, secret_store, {"openai": adapter}
            )
            created = await service.create_run(session.id, "incomplete-finish", "Please answer")
            await service.wait_for_run(created.id)
            await service.close()
            return (
                await database.get_run(created.id),
                await database.list_messages(session.id),
                await database.list_model_calls(created.id),
                await database.list_run_events(created.id),
            )

    run, messages, calls, events = asyncio.run(run())
    assert run is not None and run.status is RunStatus.FAILED
    assert run.error_code == "provider_response_incomplete"
    assert [message.role for message in messages] == ["user"]
    assert calls[0].response_snapshot is not None
    assert calls[0].response_snapshot["content"] == "The answer starts here."
    assert calls[0].response_snapshot["finish_kind"] == "incomplete"
    assert calls[0].finish_reason == reason
    assert all(event.event_type is not RunEventType.ASSISTANT_COMPLETED for event in events)
    assert all(event.event_type is not RunEventType.COMPLETED for event in events)
