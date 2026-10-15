import asyncio
import json

import httpx
import pytest

from app.application.errors import ProviderError
from app.domain.runtime import ModelMessage, ModelRequest, ModelStreamEvent
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider


def parse(provider: str, events: list[dict[str, object]]) -> list[ModelStreamEvent]:
    async def exercise() -> list[ModelStreamEvent]:
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=stream))
        ) as client:
            adapter = OpenAIProvider(client) if provider == "openai" else AnthropicProvider(client)
            request = ModelRequest(
                provider, f"{provider}:model", provider, "model", (ModelMessage("user", "Hi"),), 100
            )
            return [event async for event in adapter.stream(request, "test-key", "user")]

    return asyncio.run(exercise())


def anthropic_events(start: dict[str, object], end: dict[str, object]) -> list[dict[str, object]]:
    return [
        {"type": "message_start", "message": {"id": "message", "usage": start}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "Answer"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": end},
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("tier", ["priority", "flex", "future-served-tier", None])
def test_openai_stream_preserves_actual_served_tier_and_cache_write_usage(tier: str | None) -> None:
    events = parse(
        "openai",
        [
            {"type": "response.output_text.delta", "delta": "Answer"},
            {
                "type": "response.completed",
                "response": {
                    "id": "response",
                    "service_tier": tier,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
                    },
                },
            },
        ],
    )
    usage = next(event for event in events if event.kind == "usage")
    assert usage.service_tier == tier
    assert usage.cache_creation_tokens == 30
    assert usage.cache_creation_1h_tokens == 0


@pytest.mark.parametrize("one_hour", [0, 100, None])
def test_anthropic_stream_preserves_served_tier_and_unknown_write_ttl(one_hour: int | None) -> None:
    start: dict[str, object] = {
        "input_tokens": 100,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 300,
        "service_tier": "standard",
    }
    if one_hour is not None:
        start["cache_creation"] = {
            "ephemeral_1h_input_tokens": one_hour,
            "ephemeral_5m_input_tokens": 300 - one_hour,
        }
    events = parse("anthropic", anthropic_events(start, {"output_tokens": 10}))
    usage = next(event for event in events if event.kind == "usage")
    assert usage.service_tier == "standard"
    assert usage.cache_creation_tokens == 300
    assert usage.cache_creation_1h_tokens == one_hour


def test_anthropic_delta_updates_final_cumulative_usage_without_losing_write_ttl() -> None:
    events = parse(
        "anthropic",
        anthropic_events(
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_1h_input_tokens": 100},
                "service_tier": "priority",
            },
            {
                "input_tokens": 110,
                "output_tokens": 10,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 400,
                "cache_creation": {"ephemeral_1h_input_tokens": 200},
                "service_tier": "standard",
            },
        ),
    )
    usage = next(event for event in events if event.kind == "usage")
    assert (usage.input_tokens, usage.cached_tokens, usage.cache_creation_tokens) == (110, 40, 400)
    assert usage.cache_creation_1h_tokens == 200 and usage.service_tier == "standard"


def test_anthropic_new_write_total_without_breakdown_cannot_keep_an_old_ttl_count() -> None:
    events = parse(
        "anthropic",
        anthropic_events(
            {
                "input_tokens": 100,
                "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_1h_input_tokens": 100},
            },
            {"output_tokens": 10, "cache_creation_input_tokens": 400},
        ),
    )
    usage = next(event for event in events if event.kind == "usage")
    assert usage.cache_creation_tokens == 400 and usage.cache_creation_1h_tokens is None


def test_anthropic_zero_cache_writes_is_known_zero_without_a_ttl_breakdown() -> None:
    usage = next(
        event
        for event in parse("anthropic", anthropic_events({"input_tokens": 1}, {"output_tokens": 1}))
        if event.kind == "usage"
    )
    assert usage.cache_creation_tokens == 0 and usage.cache_creation_1h_tokens == 0


@pytest.mark.parametrize("tier", [{}, 12, "bad\nmetadata"])
def test_invalid_served_tier_is_not_silently_priced_as_standard(tier: object) -> None:
    with pytest.raises(ProviderError):
        parse(
            "openai",
            [
                {"type": "response.output_text.delta", "delta": "Answer"},
                {
                    "type": "response.completed",
                    "response": {
                        "service_tier": tier,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            ],
        )
