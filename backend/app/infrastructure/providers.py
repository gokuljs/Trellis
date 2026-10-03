import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any

import httpx

from app.application.errors import ProviderError
from app.domain.models import Message, ProviderName
from app.domain.runtime import ModelRequest, ModelStreamEvent


class OpenAIProvider:
    name: ProviderName = "openai"
    model = "gpt-5.5"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        return _stream_openai(self._client, request, api_key, user_id)

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

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        del user_id
        return _stream_anthropic(self._client, request, api_key)

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


async def _stream_openai(
    client: httpx.AsyncClient,
    request: ModelRequest,
    api_key: str,
    user_id: str,
) -> AsyncGenerator[ModelStreamEvent]:
    body = {
        "model": request.upstream_model_id,
        "input": [{"role": item.role, "content": item.content} for item in request.messages],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "medium"},
        "max_output_tokens": request.max_output_tokens,
        "safety_identifier": user_id,
    }
    response_id: str | None = None
    saw_text = False
    saw_refusal_delta = False
    completed = False
    provider_events = _post_stream(
        client,
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )

    async for payload in provider_events:
        event_type = payload.get("type")
        if event_type == "response.created":
            response = payload.get("response")
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                response_id = response["id"]
        elif event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise ProviderError(
                    "provider_invalid_response",
                    "OpenAI returned a stream Trellis could not read.",
                )
            saw_text = saw_text or bool(delta)
            yield ModelStreamEvent(kind="text_delta", text=delta)
        elif event_type == "response.refusal.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise ProviderError(
                    "provider_invalid_response",
                    "OpenAI returned a stream Trellis could not read.",
                )
            saw_refusal_delta = saw_refusal_delta or bool(delta)
            saw_text = saw_text or bool(delta)
            yield ModelStreamEvent(kind="text_delta", text=delta)
        elif event_type == "response.refusal.done":
            refusal = payload.get("refusal")
            if not saw_refusal_delta and not saw_text and isinstance(refusal, str) and refusal:
                saw_text = True
                yield ModelStreamEvent(kind="text_delta", text=refusal)
        elif event_type in {"response.completed", "response.incomplete"}:
            response = payload.get("response")
            if not isinstance(response, dict):
                raise ProviderError(
                    "provider_invalid_response",
                    "OpenAI returned a stream Trellis could not read.",
                )
            response_id_value = response.get("id")
            if isinstance(response_id_value, str):
                response_id = response_id_value
            if not saw_text:
                raise ProviderError(
                    "provider_invalid_response",
                    "OpenAI returned an empty response.",
                )
            usage = _mapping(response.get("usage"))
            input_details = _mapping(usage.get("input_tokens_details"))
            output_details = _mapping(usage.get("output_tokens_details"))
            input_tokens = _optional_integer(usage.get("input_tokens"))
            output_tokens = _optional_integer(usage.get("output_tokens"))
            cached_tokens = _optional_integer(input_details.get("cached_tokens"))
            reasoning_tokens = _optional_integer(output_details.get("reasoning_tokens"))
            usage_event = None
            if any(
                value is not None
                for value in (
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    reasoning_tokens,
                )
            ):
                usage_event = ModelStreamEvent(
                    kind="usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens,
                    cached_tokens=cached_tokens,
                    provider_response_id=response_id,
                )
            incomplete_reason = _mapping(response.get("incomplete_details")).get("reason")
            finish_reason = (
                incomplete_reason
                if event_type == "response.incomplete" and isinstance(incomplete_reason, str)
                else "incomplete"
                if event_type == "response.incomplete"
                else "completed"
            )
            completed = True
            await provider_events.aclose()
            if usage_event is not None:
                yield usage_event
            yield ModelStreamEvent(
                kind="completed",
                finish_reason=finish_reason,
                provider_response_id=response_id,
            )
            break
        elif event_type in {"response.failed", "error"}:
            raise ProviderError(
                "provider_upstream_failed",
                "OpenAI could not complete this request. Try again.",
            )

    if not completed:
        raise ProviderError(
            "provider_invalid_response",
            "OpenAI returned an incomplete stream.",
        )


async def _stream_anthropic(
    client: httpx.AsyncClient,
    request: ModelRequest,
    api_key: str,
) -> AsyncGenerator[ModelStreamEvent]:
    body = {
        "model": request.upstream_model_id,
        "max_tokens": request.max_output_tokens,
        "stream": True,
        "messages": [{"role": item.role, "content": item.content} for item in request.messages],
    }
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    finish_reason: str | None = None
    saw_text = False
    completed = False
    provider_events = _post_stream(
        client,
        "https://api.anthropic.com/v1/messages",
        headers={
            "X-Api-Key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json=body,
    )

    async for payload in provider_events:
        event_type = payload.get("type")
        if event_type == "message_start":
            message = _mapping(payload.get("message"))
            message_id = message.get("id")
            response_id = message_id if isinstance(message_id, str) else response_id
            usage = _mapping(message.get("usage"))
            input_tokens = _optional_integer(usage.get("input_tokens"))
            cached_tokens = _optional_integer(usage.get("cache_read_input_tokens"))
        elif event_type == "content_block_delta":
            delta = _mapping(payload.get("delta"))
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if not isinstance(text, str):
                    raise ProviderError(
                        "provider_invalid_response",
                        "Anthropic returned a stream Trellis could not read.",
                    )
                saw_text = saw_text or bool(text)
                yield ModelStreamEvent(kind="text_delta", text=text)
        elif event_type == "message_delta":
            delta = _mapping(payload.get("delta"))
            stop_reason = delta.get("stop_reason")
            finish_reason = stop_reason if isinstance(stop_reason, str) else finish_reason
            usage = _mapping(payload.get("usage"))
            output_tokens = _optional_integer(usage.get("output_tokens"))
        elif event_type == "message_stop":
            if not saw_text:
                raise ProviderError(
                    "provider_invalid_response",
                    "Anthropic returned an empty response.",
                )
            usage_event = None
            if any(value is not None for value in (input_tokens, output_tokens, cached_tokens)):
                usage_event = ModelStreamEvent(
                    kind="usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    provider_response_id=response_id,
                )
            completed = True
            await provider_events.aclose()
            if usage_event is not None:
                yield usage_event
            yield ModelStreamEvent(
                kind="completed",
                finish_reason=finish_reason,
                provider_response_id=response_id,
            )
            break
        elif event_type == "error":
            raise ProviderError(
                "provider_upstream_failed",
                "Anthropic could not complete this request. Try again.",
            )

    if not completed:
        raise ProviderError(
            "provider_invalid_response",
            "Anthropic returned an incomplete stream.",
        )


async def _post_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
) -> AsyncGenerator[dict[str, Any]]:
    try:
        async with client.stream("POST", url, headers=headers, json=json) as response:
            _raise_for_provider_response(response)
            async for event in _iter_sse_events(response):
                yield event
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


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield _decode_sse_data(data_lines)
                data_lines.clear()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        yield _decode_sse_data(data_lines)


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any]:
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return {"type": "stream.done"}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        raise ProviderError(
            "provider_invalid_response",
            "The provider returned a stream Trellis could not read.",
        ) from None
    if not isinstance(payload, dict):
        raise ProviderError(
            "provider_invalid_response",
            "The provider returned a stream Trellis could not read.",
        )
    return payload


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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

    _raise_for_provider_response(response)
    return response


def _raise_for_provider_response(response: httpx.Response) -> None:
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
