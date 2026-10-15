import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.application.errors import ProviderError
from app.infrastructure.providers import AnthropicProvider
from tests.test_provider_counting import request


def test_anthropic_native_compaction_preserves_signed_block_and_counts_iterations() -> None:
    block = {
        "type": "compaction",
        "content": "  Keep constraints.\n",
        "signature": "signed",
        "extra": {"future": True},
    }
    captured: list[httpx.Request] = []

    async def exercise() -> None:
        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json={
                    "id": "compact2",
                    "content": [block],
                    "stop_reason": "compaction",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "iterations": [
                            {
                                "type": "compaction",
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cache_read_input_tokens": 30,
                                "cache_creation_input_tokens": 10,
                                "cache_creation": {"ephemeral_1h_input_tokens": 6},
                                "service_tier": "standard",
                            },
                            {"type": "compaction", "input_tokens": 3, "output_tokens": 4},
                        ],
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await AnthropicProvider(client).compact(request("anthropic"), "key", "user")
        assert result.payload_json is not None and json.loads(result.payload_json) == block
        assert result.native_kind == "anthropic_block" and result.failure_code is None
        assert [event.input_tokens for event in result.usage_events] == [100, 3]
        assert [event.output_tokens for event in result.usage_events] == [20, 4]
        assert result.usage_events[0].service_tier == "standard"
        assert result.usage_events[0].cache_creation_1h_tokens == 6
        assert (
            result.usage_events[0].cached_tokens == 30
            and result.usage_events[0].cache_creation_tokens == 10
        )

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["compaction"]["type"] == "summarize" and body["max_tokens"] == 2048
    assert body["system"] == "Keep constraints." and body["tools"][0]["name"] == "read_file"
    assert captured[0].headers["anthropic-beta"] == "compact-2026-09-04" and "stream" not in body


def test_native_payload_over_limit_is_discarded_whole_but_usage_is_preserved() -> None:
    async def exercise() -> None:
        payload = {
            "stop_reason": "compaction",
            "content": [{"type": "compaction", "content": "x" * 262145, "signature": "signed"}],
            "usage": {
                "iterations": [{"type": "compaction", "input_tokens": 100, "output_tokens": 10}]
            },
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        ) as client:
            adapter = AnthropicProvider(client)
            result = await adapter.compact(request("anthropic"), "key", "user")
        assert result.payload_json is None and result.failure_code == "native_context_too_large"
        assert result.usage_events[0].input_tokens == 100

    asyncio.run(exercise())


@pytest.mark.parametrize("reason", ["max_tokens", "tool_use", "refusal", "end_turn"])
def test_incomplete_native_summary_preserves_usage_for_portable_fallback(reason: str) -> None:
    async def exercise() -> None:
        payload = {
            "stop_reason": reason,
            "content": [],
            "usage": {
                "iterations": [{"type": "compaction", "input_tokens": 99, "output_tokens": 10}]
            },
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        ) as client:
            result = await AnthropicProvider(client).compact(request("anthropic"), "key", "user")
        assert result.payload_json is None and result.failure_code is not None
        assert result.usage_events[0].input_tokens == 99

    asyncio.run(exercise())


def test_native_context_is_replayed_first_on_count_requests() -> None:
    captured: list[httpx.Request] = []
    native = {"type": "compaction", "content": "Saved", "signature": "signed"}

    async def exercise() -> None:
        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"input_tokens": 90})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = AnthropicProvider(client)
            await adapter.count_input(
                replace(request("anthropic"), native_context_json=json.dumps(native)), "key", "user"
            )

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["messages"][0] == {"role": "assistant", "content": [native]}
    assert body["messages"][1]["content"] == "Continue"
    assert captured[0].headers["anthropic-beta"] == "compact-2026-09-04"


def test_invalid_native_usage_discards_state_but_keeps_valid_reported_iterations() -> None:
    async def exercise() -> None:
        payload = {
            "stop_reason": "compaction",
            "content": [{"type": "compaction", "content": "Summary", "signature": "signed"}],
            "usage": {
                "iterations": [
                    {"type": "compaction", "input_tokens": 99, "output_tokens": 10},
                    {"type": "compaction", "input_tokens": -1, "output_tokens": 3},
                ]
            },
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        ) as client:
            result = await AnthropicProvider(client).compact(request("anthropic"), "key", "user")
        assert result.payload_json is None and result.failure_code == "native_usage_unknown"
        assert result.usage_events[0].input_tokens == 99

    asyncio.run(exercise())


def test_native_signed_state_reaches_generation_without_modification() -> None:
    captured: list[httpx.Request] = []
    native = {"type": "compaction", "content": "  Saved.\n", "signature": "signed"}
    events = [
        {"type": "message_start", "message": {"id": "res", "usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Next"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]

    async def exercise() -> None:
        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200, content="".join(f"data: {json.dumps(event)}\n\n" for event in events)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = AnthropicProvider(client)
            result = [
                event
                async for event in adapter.stream(
                    replace(request("anthropic"), native_context_json=json.dumps(native)),
                    "key",
                    "user",
                )
            ]
        assert result[-1].finish_kind == "final"

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["messages"][0] == {"role": "assistant", "content": [native]}
    assert captured[0].headers["anthropic-beta"] == "compact-2026-09-04"


def test_cancellation_during_native_response_closes_the_http_stream() -> None:
    class SlowBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            yield b'{"output":'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    async def exercise() -> None:
        body = SlowBody()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=body))
        ) as client:
            task = asyncio.create_task(
                AnthropicProvider(client).compact(request("anthropic"), "key", "user")
            )
            await body.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert body.closed

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "native",
    [
        "[",
        '{"type":"compaction","content":"summary"}',
        '{"type":"compaction","content":"summary","signature":""}',
        '[{"type":"compaction","encrypted_content":"opaque"}]',
    ],
)
def test_unsigned_or_wrong_provider_state_is_rejected_before_anthropic_request(native: str) -> None:
    sent = []

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: sent.append(req) or httpx.Response(200, json={"input_tokens": 1})
            )
        ) as client:
            with pytest.raises(ProviderError):
                await AnthropicProvider(client).count_input(
                    replace(request("anthropic"), native_context_json=native), "key", "user"
                )

    asyncio.run(exercise())
    assert sent == []
