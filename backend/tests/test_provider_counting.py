import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.application.context_budget import input_fingerprint
from app.application.errors import ProviderError
from app.domain.context import InputTokenCount
from app.domain.runtime import ModelMessage, ModelRequest, ModelToolSpec
from app.infrastructure import providers
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider


def request(provider: str) -> ModelRequest:
    return ModelRequest(
        provider,
        f"{provider}:model",
        provider,
        "test-model",
        (ModelMessage("user", "Continue"),),
        2048,
        "Keep constraints.",
        (ModelToolSpec("read_file", "Read a file", {"type": "object"}),),
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_count_endpoint_uses_generation_messages_instructions_and_tools(provider: str) -> None:
    captured: list[httpx.Request] = []

    async def exercise() -> None:
        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"input_tokens": 75})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = OpenAIProvider(client) if provider == "openai" else AnthropicProvider(client)
            result = await adapter.count_input(request(provider), "test-key", "user")
        assert result == InputTokenCount(75, "provider", input_fingerprint(request(provider)))

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["model"] == "test-model" and body["tools"][0]["name"] == "read_file"
    if provider == "openai":
        assert captured[0].url.path == "/v1/responses/input_tokens"
        assert body["input"] == [{"role": "user", "content": "Continue"}]
        assert body["instructions"] == "Keep constraints."
    else:
        assert captured[0].url.path == "/v1/messages/count_tokens"
        assert body["messages"] == [{"role": "user", "content": "Continue"}]
        assert body["system"] == "Keep constraints."
    assert "stream" not in body and "max_tokens" not in body and "max_output_tokens" not in body


@pytest.mark.parametrize("value", [-1, True, 1.5, "90", None])
def test_provider_count_rejects_invalid_token_counts(value: object) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"input_tokens": value})
            )
        ) as client:
            with pytest.raises(ProviderError):
                await OpenAIProvider(client).count_input(request("openai"), "key", "user")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content="private provider body"),
        httpx.Response(200, json=[]),
        httpx.Response(503, json={"error": "private provider body"}),
        httpx.Response(200, content=b"x" * 16385),
    ],
)
def test_count_responses_are_bounded_and_errors_do_not_echo_provider_body(
    response: httpx.Response,
) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
            with pytest.raises(ProviderError) as error:
                await OpenAIProvider(client).count_input(request("openai"), "key", "user")
            assert "private" not in error.value.message

    asyncio.run(exercise())


def test_count_deadline_covers_the_entire_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "_CONTEXT_REQUEST_TIMEOUT_SECONDS", 0.01, raising=False)

    async def respond(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"input_tokens": 75})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), timeout=None
        ) as client:
            with pytest.raises(ProviderError) as error:
                await OpenAIProvider(client).count_input(request("openai"), "key", "user")
            assert error.value.code == "provider_timeout"

    asyncio.run(exercise())


def test_cancelling_count_closes_the_http_stream() -> None:
    started: asyncio.Event
    closed = False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"input_tokens":'
            started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def exercise() -> None:
        nonlocal started
        started = asyncio.Event()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Body())),
            timeout=None,
        ) as client:
            task = asyncio.create_task(
                OpenAIProvider(client).count_input(request("openai"), "key", "user")
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed

    asyncio.run(exercise())
