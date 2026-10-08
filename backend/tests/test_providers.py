import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from app.domain.models import Message, MessageRole
from app.domain.runtime import (
    ModelContinuationItem,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ModelToolSpec,
)
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


def anthropic_event(event_type: str, **fields: object) -> tuple[str, dict[str, object]]:
    return event_type, {"type": event_type, **fields}


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


def test_openai_stream_sends_tools_instructions_and_replays_tool_exchange() -> None:
    async def run() -> httpx.Request:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {"type": "response.output_text.delta", "delta": "Done"},
                    {"type": "response.completed", "response": {"id": "resp_done"}},
                ),
            )

        request = replace(
            model_request("openai", "openai", "future-open-model"),
            system_instructions="Work carefully.",
            tools=(
                ModelToolSpec(
                    name="read_file",
                    description="Read a workspace file.",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                ),
            ),
            messages=(
                ModelMessage(role="user", content="Read README"),
                ModelMessage(
                    role="assistant",
                    content="I will read it.",
                    tool_calls=(
                        ModelToolCall(
                            id="call_1", name="read_file", arguments={"path": "README.md"}
                        ),
                    ),
                ),
                ModelMessage(role="tool", content="Trellis", tool_call_id="call_1"),
            ),
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [event async for event in OpenAIProvider(client).stream(request, "sk", "user")]
        assert events[-1].kind == "completed"
        return captured[0]

    payload = json.loads(asyncio.run(run()).content)

    assert payload["instructions"] == "Work carefully."
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    assert payload["input"] == [
        {"role": "user", "content": "Read README"},
        {"role": "assistant", "content": "I will read it."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "Trellis"},
    ]


def test_openai_stream_marks_optional_tool_schema_non_strict() -> None:
    async def run() -> httpx.Request:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {"type": "response.output_text.delta", "delta": "Done"},
                    {"type": "response.completed", "response": {"id": "resp_done"}},
                ),
            )

        request = replace(
            model_request("openai", "openai", "future-open-model"),
            tools=(
                ModelToolSpec(
                    name="read_file",
                    description="Read a file.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer"},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                ),
            ),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _event in OpenAIProvider(client).stream(request, "sk", "user"):
                pass
        return captured[0]

    payload = json.loads(asyncio.run(run()).content)
    assert payload["tools"][0]["strict"] is False


def test_openai_stream_assembles_multiple_function_calls_after_response_completion() -> None:
    async def run() -> list[ModelStreamEvent]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {"type": "response.created", "response": {"id": "resp_tools"}},
                    {"type": "response.output_text.delta", "delta": "Checking."},
                    {
                        "type": "response.output_item.added",
                        "output_index": 1,
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 2,
                        "item": {
                            "type": "function_call",
                            "id": "item_2",
                            "call_id": "call_2",
                            "name": "inspect_git",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 2,
                        "item_id": "item_2",
                        "delta": '{"operation":"status"}',
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 1,
                        "item_id": "item_1",
                        "delta": '{"path":"REA',
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 1,
                        "item_id": "item_1",
                        "delta": 'DME.md"}',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 2,
                        "item_id": "item_2",
                        "name": "inspect_git",
                        "arguments": '{"operation":"status"}',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 1,
                        "item_id": "item_1",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 1,
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_tools",
                            "usage": {"input_tokens": 12, "output_tokens": 8},
                        },
                    },
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in OpenAIProvider(client).stream(
                    model_request("openai", "openai", "future-open-model"), "sk", "user"
                )
            ]

    assert asyncio.run(run()) == [
        ModelStreamEvent(kind="text_delta", text="Checking."),
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(id="call_1", name="read_file", arguments={"path": "README.md"}),
        ),
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(
                id="call_2", name="inspect_git", arguments={"operation": "status"}
            ),
        ),
        ModelStreamEvent(
            kind="usage", input_tokens=12, output_tokens=8, provider_response_id="resp_tools"
        ),
        ModelStreamEvent(
            kind="completed", finish_reason="completed", provider_response_id="resp_tools"
        ),
    ]


def test_openai_stream_accepts_tool_only_completion() -> None:
    async def run() -> list[ModelStreamEvent]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "inspect_git",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "item_id": "item_1",
                        "arguments": "{}",
                    },
                    {"type": "response.completed", "response": {"id": "resp_tool_only"}},
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in OpenAIProvider(client).stream(
                    model_request("openai", "openai", "future-open-model"), "sk", "user"
                )
            ]

    assert asyncio.run(run()) == [
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(id="call_1", name="inspect_git", arguments={}),
        ),
        ModelStreamEvent(
            kind="completed", finish_reason="completed", provider_response_id="resp_tool_only"
        ),
    ]


def test_openai_stream_replays_final_encrypted_reasoning_with_tool_result() -> None:
    async def run() -> tuple[list[ModelStreamEvent], httpx.Request]:
        captured: list[httpx.Request] = []
        final_reasoning = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque-final",
            "status": "completed",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if len(captured) == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=sse_data(
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "type": "reasoning",
                                "id": "rs_1",
                                "summary": [],
                                "encrypted_content": "partial",
                            },
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": final_reasoning,
                        },
                        {"type": "response.output_text.delta", "delta": "Checking."},
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_1",
                                "name": "read_file",
                                "arguments": "",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            "output_index": 1,
                            "item_id": "fc_1",
                            "arguments": '{"path":"README.md"}',
                        },
                        {"type": "response.completed", "response": {"id": "resp_1"}},
                    ),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {"type": "response.output_text.delta", "delta": "Done"},
                    {"type": "response.completed", "response": {"id": "resp_2"}},
                ),
            )

        request = model_request("openai", "openai", "future-open-model")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIProvider(client)
            first_events = [event async for event in provider.stream(request, "sk", "user")]
            assert json.loads(captured[0].content)["include"] == ["reasoning.encrypted_content"]
            continuation = next(
                event.continuation_item
                for event in first_events
                if event.kind == "continuation_item"
            )
            tool_call = next(event.tool_call for event in first_events if event.kind == "tool_call")
            assert continuation is not None
            assert tool_call is not None
            second_request = replace(
                request,
                messages=(
                    *request.messages,
                    ModelMessage(
                        role="assistant",
                        content="Checking.",
                        tool_calls=(tool_call,),
                        continuation_items=(continuation,),
                    ),
                    ModelMessage(role="tool", content="contents", tool_call_id=tool_call.id),
                ),
            )
            second_events = [event async for event in provider.stream(second_request, "sk", "user")]
            assert second_events[-1].kind == "completed"

        assert isinstance(continuation, ModelContinuationItem)
        assert continuation.provider_id == request.provider_id
        assert continuation.model_id == request.model_id
        assert json.loads(continuation.payload_json) == final_reasoning
        return first_events, captured[1]

    first_events, second_http_request = asyncio.run(run())
    assert [event.kind for event in first_events] == [
        "text_delta",
        "continuation_item",
        "tool_call",
        "completed",
    ]
    second_payload = json.loads(second_http_request.content)
    assert second_payload["input"] == [
        {"role": "user", "content": "Hello"},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque-final",
            "status": "completed",
        },
        {"role": "assistant", "content": "Checking."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "contents"},
    ]


def test_openai_stream_rejects_unfinished_reasoning_before_tool_execution() -> None:
    seen: list[ModelStreamEvent] = []

    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "reasoning",
                            "id": "rs_1",
                            "encrypted_content": "partial",
                        },
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 1,
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 1,
                        "item_id": "fc_1",
                        "arguments": "{}",
                    },
                    {"type": "response.completed", "response": {"id": "resp_1"}},
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "future-open-model"), "sk", "user"
            ):
                seen.append(event)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_invalid_response"
    assert seen == []


@pytest.mark.parametrize("payload_json", ["{", "\ud800"])
def test_openai_stream_rejects_corrupt_continuation_before_request(payload_json: str) -> None:
    async def run() -> None:
        continuation = ModelContinuationItem(
            provider_id="openai",
            model_id="openai:test-model",
            payload_json=payload_json,
        )
        request = replace(
            model_request("openai", "openai", "future-open-model"),
            messages=(
                ModelMessage(role="assistant", content="", continuation_items=(continuation,)),
            ),
        )

        def unexpected_request(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("invalid continuation must not reach the provider")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            async for _event in OpenAIProvider(client).stream(request, "sk", "user"):
                pass

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_invalid_response"


@pytest.mark.parametrize(
    "events",
    [
        # Arguments must be one complete JSON object.
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "name": "read_file",
                "arguments": "{",
            },
        ],
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "name": "read_file",
                "arguments": "[]",
            },
        ],
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "name": "read_file",
                "arguments": '{"line":NaN}',
            },
        ],
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "arguments": '{"x":' + "[" * 100000 + "0" + "]" * 100000 + "}",
            },
        ],
        [
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "item_1",
                "delta": "\ud800",
            },
        ],
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "item_1",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{}",
                    "status": "incomplete",
                },
            },
        ],
        # A duplicate finalization must not execute the call twice.
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "name": "read_file",
                "arguments": "{}",
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item_1",
                "name": "read_file",
                "arguments": "{}",
            },
        ],
        # A declared call without finalized arguments is truncated.
        [],
        [
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "item_1",
                "delta": "x" * (256 * 1024 + 1),
            },
        ],
    ],
)
def test_openai_stream_rejects_invalid_function_calls_without_emitting_them(
    events: list[dict[str, object]],
) -> None:
    seen: list[ModelStreamEvent] = []

    async def run() -> list[ModelStreamEvent]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    *events,
                    {"type": "response.completed", "response": {"id": "resp_bad"}},
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "future-open-model"), "sk", "user"
            ):
                seen.append(event)
        return seen

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_invalid_response"
    assert all(event.kind != "tool_call" for event in seen)


def test_openai_stream_does_not_emit_a_tool_call_before_provider_completion() -> None:
    seen: list[ModelStreamEvent] = []

    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_data(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "item_1",
                            "call_id": "call_1",
                            "name": "inspect_git",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "item_id": "item_1",
                        "name": "inspect_git",
                        "arguments": "{}",
                    },
                    {"type": "response.failed", "response": {"error": {"message": "private"}}},
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for event in OpenAIProvider(client).stream(
                model_request("openai", "openai", "future-open-model"), "sk", "user"
            ):
                seen.append(event)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_upstream_failed"
    assert seen == []


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
                                    "cache_creation_input_tokens": 3,
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
            cache_creation_tokens=3,
            provider_response_id="msg_456",
        ),
        ModelStreamEvent(
            kind="completed",
            finish_reason="end_turn",
            provider_response_id="msg_456",
        ),
    ]


def test_anthropic_stream_replays_tool_exchange_and_assembles_mixed_blocks() -> None:
    async def run() -> tuple[list[ModelStreamEvent], httpx.Request]:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_named(
                    anthropic_event("message_start", message={"id": "msg_tools"}),
                    anthropic_event(
                        "content_block_start", index=0, content_block={"type": "text", "text": ""}
                    ),
                    anthropic_event(
                        "content_block_delta",
                        index=0,
                        delta={"type": "text_delta", "text": "I'll inspect both."},
                    ),
                    anthropic_event("content_block_stop", index=0),
                    anthropic_event(
                        "content_block_start",
                        index=1,
                        content_block={
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "read_file",
                            "input": {},
                        },
                    ),
                    anthropic_event(
                        "content_block_delta",
                        index=1,
                        delta={"type": "input_json_delta", "partial_json": '{"path":'},
                    ),
                    anthropic_event(
                        "content_block_delta",
                        index=1,
                        delta={"type": "input_json_delta", "partial_json": '"README.md"}'},
                    ),
                    anthropic_event("content_block_stop", index=1),
                    anthropic_event(
                        "content_block_start",
                        index=2,
                        content_block={
                            "type": "tool_use",
                            "id": "toolu_2",
                            "name": "inspect_git",
                            "input": {},
                        },
                    ),
                    anthropic_event("content_block_stop", index=2),
                    anthropic_event(
                        "message_delta",
                        delta={"stop_reason": "tool_use"},
                        usage={"output_tokens": 19},
                    ),
                    anthropic_event("message_stop"),
                ),
            )

        request = ModelRequest(
            provider_id="anthropic",
            model_id="anthropic:test-model",
            adapter_kind="anthropic",
            upstream_model_id="future-claude-model",
            messages=(
                ModelMessage(role="user", content="Inspect the repository"),
                ModelMessage(
                    role="assistant",
                    content="I will check.",
                    tool_calls=(
                        ModelToolCall(id="prior_1", name="read_file", arguments={"path": "a.py"}),
                        ModelToolCall(id="prior_2", name="inspect_git", arguments={}),
                    ),
                ),
                ModelMessage(role="tool", content="file contents", tool_call_id="prior_1"),
                ModelMessage(role="tool", content="clean", tool_call_id="prior_2"),
            ),
            max_output_tokens=256,
            system_instructions="Work carefully.",
            tools=(
                ModelToolSpec(
                    name="read_file",
                    description="Read one file",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                ),
            ),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                event async for event in AnthropicProvider(client).stream(request, "sk-ant", "user")
            ]
        return events, captured[0]

    events, sent = asyncio.run(run())
    payload = json.loads(sent.content)
    assert payload["system"] == "Work carefully."
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read one file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert payload["messages"] == [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will check."},
                {
                    "type": "tool_use",
                    "id": "prior_1",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                },
                {"type": "tool_use", "id": "prior_2", "name": "inspect_git", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "prior_1", "content": "file contents"},
                {"type": "tool_result", "tool_use_id": "prior_2", "content": "clean"},
            ],
        },
    ]
    assert events == [
        ModelStreamEvent(kind="text_delta", text="I'll inspect both."),
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(
                id="toolu_1", name="read_file", arguments={"path": "README.md"}
            ),
        ),
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(id="toolu_2", name="inspect_git", arguments={}),
        ),
        ModelStreamEvent(
            kind="usage",
            output_tokens=19,
            cache_creation_tokens=0,
            provider_response_id="msg_tools",
        ),
        ModelStreamEvent(
            kind="completed", finish_reason="tool_use", provider_response_id="msg_tools"
        ),
    ]


def test_anthropic_stream_accepts_tool_only_completion() -> None:
    async def run() -> list[ModelStreamEvent]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_named(
                    anthropic_event("message_start", message={"id": "msg_only"}),
                    anthropic_event(
                        "content_block_start",
                        index=0,
                        content_block={
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "inspect_git",
                            "input": {},
                        },
                    ),
                    anthropic_event("content_block_stop", index=0),
                    anthropic_event("message_delta", delta={"stop_reason": "tool_use"}),
                    anthropic_event("message_stop"),
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                event
                async for event in AnthropicProvider(client).stream(
                    model_request("anthropic", "anthropic", "future-claude-model"), "sk", "user"
                )
            ]

    assert asyncio.run(run()) == [
        ModelStreamEvent(
            kind="tool_call",
            tool_call=ModelToolCall(id="toolu_1", name="inspect_git", arguments={}),
        ),
        ModelStreamEvent(
            kind="completed", finish_reason="tool_use", provider_response_id="msg_only"
        ),
    ]


@pytest.mark.parametrize("case", ["malformed_json", "unclosed_block", "max_tokens"])
def test_anthropic_stream_rejects_incomplete_tool_use_before_emitting_call(case: str) -> None:
    seen: list[ModelStreamEvent] = []
    partial_json = '{"path":' if case == "malformed_json" else '{"path":"x"}'
    events = [
        anthropic_event("message_start", message={"id": "msg"}),
        anthropic_event(
            "content_block_start",
            index=0,
            content_block={"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
        ),
        anthropic_event(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": partial_json},
        ),
    ]
    if case != "unclosed_block":
        events.append(anthropic_event("content_block_stop", index=0))
    reason = "max_tokens" if case == "max_tokens" else "tool_use"
    events.extend(
        [
            anthropic_event("message_delta", delta={"stop_reason": reason}),
            anthropic_event("message_stop"),
        ]
    )

    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_named(*events),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for event in AnthropicProvider(client).stream(
                model_request("anthropic", "anthropic", "future-claude-model"), "sk", "user"
            ):
                seen.append(event)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_invalid_response"
    assert seen == []


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_index",
        "duplicate_id",
        "unknown_index",
        "oversize_arguments",
        "duplicate_stop",
        "delta_after_stop",
        "unclosed_text",
        "truncated_stream",
    ],
)
def test_anthropic_stream_rejects_invalid_tool_blocks_before_emitting_call(case: str) -> None:
    seen: list[ModelStreamEvent] = []

    def start(index: int, call_id: str) -> tuple[str, dict[str, object]]:
        return (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "read_file",
                    "input": {},
                },
            },
        )

    def stop(index: int) -> tuple[str, dict[str, object]]:
        return ("content_block_stop", {"type": "content_block_stop", "index": index})

    def input_delta(index: int, value: str) -> tuple[str, dict[str, object]]:
        return (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": value},
            },
        )

    events: list[tuple[str, dict[str, object]]] = [start(0, "toolu_1")]
    if case == "duplicate_index":
        events.append(start(0, "toolu_2"))
    elif case == "duplicate_id":
        events.extend([stop(0), start(1, "toolu_1"), stop(1)])
    elif case == "unknown_index":
        events.append(input_delta(1, "{}"))
    elif case == "oversize_arguments":
        events.append(input_delta(0, '{"x":"' + "a" * (256 * 1024) + '"}'))
    elif case == "duplicate_stop":
        events.append(stop(0))
    elif case == "delta_after_stop":
        events.extend([stop(0), input_delta(0, "{}")])
    elif case == "unclosed_text":
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
    if case != "duplicate_id":
        events.append(stop(0))
    if case != "truncated_stream":
        events.extend(
            [
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        )

    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_named(
                    ("message_start", {"type": "message_start", "message": {"id": "msg"}}),
                    *events,
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for event in AnthropicProvider(client).stream(
                model_request("anthropic", "anthropic", "future-claude-model"), "sk", "user"
            ):
                seen.append(event)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.code == "provider_invalid_response"
    assert seen == []


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
