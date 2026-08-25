from collections.abc import Sequence
from typing import Any

import httpx

from app.domain.models import Message, ProviderName


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OpenAIProvider:
    name: ProviderName = "openai"
    model = "gpt-5.5"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        response = await _post(
            self._client,
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": self.model,
                "input": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "store": False,
                "reasoning": {"effort": "medium"},
                "max_output_tokens": 4096,
                "safety_identifier": user_id,
            },
        )
        try:
            payload = response.json()
            for item in payload["output"]:
                if item.get("type") != "message" or item.get("role") != "assistant":
                    continue
                text = "".join(
                    block.get("text", "")
                    for block in item.get("content", [])
                    if block.get("type") == "output_text"
                )
                if text:
                    return text
        except KeyError, TypeError, ValueError:
            pass
        raise ProviderError(
            "provider_invalid_response",
            "OpenAI returned a response Trellis could not read.",
        )


class AnthropicProvider:
    name: ProviderName = "anthropic"
    model = "claude-sonnet-5"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        del user_id
        response = await _post(
            self._client,
            "https://api.anthropic.com/v1/messages",
            headers={
                "X-Api-Key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
            },
        )
        try:
            payload = response.json()
            text = "".join(
                block.get("text", "") for block in payload["content"] if block.get("type") == "text"
            )
            if text:
                return text
        except KeyError, TypeError, ValueError:
            pass
        raise ProviderError(
            "provider_invalid_response",
            "Anthropic returned a response Trellis could not read.",
        )


async def _post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
) -> httpx.Response:
    try:
        response = await client.post(url, headers=headers, json=json)
    except httpx.TimeoutException:
        raise ProviderError(
            "provider_timeout",
            "The provider took too long to respond. Try again.",
        ) from None
    except httpx.RequestError:
        raise ProviderError(
            "provider_upstream_failed",
            "Trellis could not reach the provider. Check your connection and try again.",
        ) from None

    if response.status_code in {401, 403}:
        raise ProviderError(
            "provider_auth_failed",
            "The provider rejected this API key. Update it in Settings.",
        )
    if response.status_code == 429:
        raise ProviderError(
            "provider_rate_limited",
            "The provider rate limit was reached. Try again shortly.",
        )
    if response.is_error:
        raise ProviderError(
            "provider_upstream_failed",
            "The provider could not complete this request. Try again.",
        )
    return response
