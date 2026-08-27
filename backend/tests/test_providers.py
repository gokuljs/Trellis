import asyncio
import json

import httpx
import pytest

from app.domain.models import Message, MessageRole
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
