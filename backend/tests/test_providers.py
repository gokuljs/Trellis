import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.domain.models import Message, MessageRole
from app.domain.runtime import ModelMessage, ModelRequest, ModelStreamEvent
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider, ProviderError


def message(message_id: str, ordinal: int, role: MessageRole, content: str) -> Message:
    return Message(
        id=message_id,
        session_id="session-1",
        turn_id=f"turn-{ordinal}",
        ordinal=ordinal,
        role=role,
        content=content,
        provider=None,
        model=None,
        created_at="2026-08-25T00:00:00Z",
    )


def model_request(adapter_kind: str, provider_id: str, upstream_model_id: str) -> ModelRequest:
    return ModelRequest(
        provider_id=provider_id,
        model_id=f"{provider_id}:test-model",
        adapter_kind=adapter_kind,
        upstream_model_id=upstream_model_id,
        messages=(ModelMessage(role="user", content="Hello"),),
        max_output_tokens=256,
    )


def sse_data(*events: dict[str, object]) -> bytes:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events).encode() + b"\n\n"


def sse_named(*events: tuple[str, dict[str, object]]) -> bytes:
    return (
        "\n\n".join(f"event: {name}\ndata: {json.dumps(event)}" for name, event in events).encode()
        + b"\n\n"
    )


def test_openai_provider_uses_stateless_responses_contract() -> None:
    async def run() -> tuple[str, httpx.Request]:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Mapped reply"}],
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenAIProvider(client).complete(
                [message("m1", 1, "user", "Hello")],
                "sk-openai-secret",
                "installation-id",
            )
        return result, captured[0]

    result, request = asyncio.run(run())
    payload = json.loads(request.content)

    assert result == "Mapped reply"
    assert request.url == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer sk-openai-secret"
    assert payload == {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "Hello"}],
        "store": False,
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 4096,
        "safety_identifier": "installation-id",
    }


def test_anthropic_provider_uses_stateless_messages_contract() -> None:
    async def run() -> tuple[str, httpx.Request]:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "Claude reply"}]},
            )

        history = [
            message("m1", 1, "user", "Hello"),
            message("m2", 2, "assistant", "Hi"),
            message("m3", 3, "user", "Continue"),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await AnthropicProvider(client).complete(
                history,
                "sk-ant-secret",
                "installation-id",
            )
        return result, captured[0]

    result, request = asyncio.run(run())
    payload = json.loads(request.content)

    assert result == "Claude reply"
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert payload == {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Continue"},
        ],
    }


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (500, "provider_upstream_failed"),
    ],
)
def test_provider_http_errors_are_sanitized(status_code: int, expected_code: str) -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text="upstream leaked secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await OpenAIProvider(client).complete(
                [message("m1", 1, "user", "Hello")],
                "sk-do-not-leak",
                "installation-id",
            )

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())

    assert raised.value.code == expected_code
    assert "upstream leaked secret" not in str(raised.value)
    assert "sk-do-not-leak" not in str(raised.value)


def test_provider_timeout_is_sanitized() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out with sk-do-not-leak", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await AnthropicProvider(client).complete(
                [message("m1", 1, "user", "Hello")],
                "sk-do-not-leak",
                "installation-id",
            )

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())

    assert raised.value.code == "provider_timeout"
    assert "sk-do-not-leak" not in str(raised.value)


def test_openai_stream_normalizes_deltas_usage_and_model_snapshot() -> None:
    async def run() -> tuple[list[ModelStreamEvent], httpx.Request]:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {"type": "response.created", "response": {"id": "resp_123"}},
                    {"type": "response.output_text.delta", "delta": "Hello"},
                    {"type": "response.output_text.delta", "delta": " world"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_123",
                            "status": "completed",
                            "usage": {
                                "input_tokens": 5,
                                "output_tokens": 3,
                                "input_tokens_details": {"cached_tokens": 2},
                                "output_tokens_details": {"reasoning_tokens": 1},
                            },
                        },
                    },
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                event
                async for event in OpenAIProvider(client).stream(
                    model_request("openai", "openai", "future-open-model"),
                    "sk-openai-stream",
                    "installation-id",
                )
            ]
        return events, captured[0]

    events, request = asyncio.run(run())
    payload = json.loads(request.content)

    assert request.url == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer sk-openai-stream"
    assert payload["model"] == "future-open-model"
    assert payload["stream"] is True
    assert payload["input"] == [{"role": "user", "content": "Hello"}]
    assert events == [
        ModelStreamEvent(kind="text_delta", text="Hello"),
        ModelStreamEvent(kind="text_delta", text=" world"),
        ModelStreamEvent(
            kind="usage",
            input_tokens=5,
            output_tokens=3,
            reasoning_tokens=1,
            cached_tokens=2,
            provider_response_id="resp_123",
        ),
        ModelStreamEvent(
            kind="completed",
            finish_reason="completed",
            provider_response_id="resp_123",
        ),
    ]


def test_openai_refusal_stream_is_preserved_as_assistant_text() -> None:
    async def run() -> list[ModelStreamEvent]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.refusal.delta",
                        "delta": "I can't help with that request.",
                    },
                    {
                        "type": "response.refusal.done",
                        "refusal": "I can't help with that request.",
                    },
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_refusal", "status": "completed"},
                    },
                    {
                        "type": "response.failed",
                        "response": {"error": {"message": "trailing event must be ignored"}},
                    },
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in OpenAIProvider(client).stream(
                    model_request("openai", "openai", "future-open-model"),
                    "sk-openai-stream",
                    "installation-id",
                )
            ]

    events = asyncio.run(run())

    assert events == [
        ModelStreamEvent(kind="text_delta", text="I can't help with that request."),
        ModelStreamEvent(
            kind="completed",
            finish_reason="completed",
            provider_response_id="resp_refusal",
        ),
    ]


def test_anthropic_stream_normalizes_sse_events_and_cumulative_usage() -> None:
    async def run() -> tuple[list[ModelStreamEvent], httpx.Request]:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_named(
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg_456",
                                "usage": {
                                    "input_tokens": 7,
                                    "cache_read_input_tokens": 2,
                                },
                            },
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "Claude"},
                        },
                    ),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 4},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                event
                async for event in AnthropicProvider(client).stream(
                    model_request("anthropic", "anthropic", "future-claude-model"),
                    "sk-ant-stream",
                    "installation-id",
                )
            ]
        return events, captured[0]

    events, request = asyncio.run(run())
    payload = json.loads(request.content)

    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-stream"
    assert payload["model"] == "future-claude-model"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 256
    assert events == [
        ModelStreamEvent(kind="text_delta", text="Claude"),
        ModelStreamEvent(
            kind="usage",
            input_tokens=7,
            output_tokens=4,
            cached_tokens=2,
            provider_response_id="msg_456",
        ),
        ModelStreamEvent(
            kind="completed",
            finish_reason="end_turn",
            provider_response_id="msg_456",
        ),
    ]


def test_openai_stream_rejects_malformed_and_truncated_events_safely() -> None:
    async def run(body: bytes) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "gpt-compatible-model"),
                "sk-do-not-leak",
                "installation-id",
            ):
                pass

    with pytest.raises(ProviderError, match="returned a stream Trellis could not read"):
        asyncio.run(run(b"data: {not-json}\n\n"))
    with pytest.raises(ProviderError, match="OpenAI returned an incomplete stream"):
        asyncio.run(run(sse_data({"type": "response.output_text.delta", "delta": "partial"})))


def test_provider_stream_errors_are_sanitized() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.failed",
                        "response": {"error": {"message": "private sk-provider-secret"}},
                    }
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "gpt-compatible-model"),
                "sk-provider-secret",
                "installation-id",
            ):
                pass

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())

    assert raised.value.code == "provider_upstream_failed"
    assert "private sk-provider-secret" not in str(raised.value)
    assert "sk-provider-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (503, "provider_upstream_failed"),
    ],
)
def test_provider_stream_http_errors_are_sanitized(
    status_code: int,
    expected_code: str,
) -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text="private sk-provider-secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _event in AnthropicProvider(client).stream(
                model_request("anthropic", "anthropic", "claude-compatible-model"),
                "sk-provider-secret",
                "installation-id",
            ):
                pass

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())

    assert raised.value.code == expected_code
    assert "private sk-provider-secret" not in str(raised.value)
    assert "sk-provider-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.ReadTimeout, "provider_timeout"),
        (httpx.ReadError, "provider_upstream_failed"),
    ],
)
def test_midstream_transport_failures_are_sanitized(
    exception_type: type[httpx.RequestError],
    expected_code: str,
) -> None:
    class FailingStream(httpx.AsyncByteStream):
        def __init__(self, error: httpx.RequestError) -> None:
            self.error = error

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse_data({"type": "response.output_text.delta", "delta": "partial"})
            raise self.error

        async def aclose(self) -> None:
            pass

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if exception_type is httpx.ReadTimeout:
                error = httpx.ReadTimeout("timeout with sk-provider-secret", request=request)
            else:
                error = httpx.ReadError("socket failed with sk-provider-secret", request=request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=FailingStream(error),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "gpt-compatible-model"),
                "sk-provider-secret",
                "installation-id",
            ):
                pass

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())

    assert raised.value.code == expected_code
    assert "sk-provider-secret" not in str(raised.value)


def test_provider_stream_preserves_cancellation_and_closes_upstream() -> None:
    class HangingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.closed = False
            self.release = asyncio.Event()

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse_data({"type": "response.output_text.delta", "delta": "Hello"})
            self.entered.set()
            await self.release.wait()

        async def aclose(self) -> None:
            self.closed = True

    async def run() -> None:
        stream = HangingStream()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = OpenAIProvider(client).stream(
                model_request("openai", "openai", "gpt-compatible-model"),
                "sk-openai-stream",
                "installation-id",
            )
            first = await anext(events)
            assert first == ModelStreamEvent(kind="text_delta", text="Hello")

            async def next_event() -> ModelStreamEvent:
                return await anext(events)

            pending = asyncio.create_task(next_event())
            await stream.entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await events.aclose()

        assert stream.closed

    asyncio.run(run())
