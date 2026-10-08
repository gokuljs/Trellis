import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.application.errors import ProviderError
from app.domain.models import Message, ProviderName
from app.domain.runtime import (
    ModelContinuationItem,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
)

_MAX_OPENAI_TOOL_CALLS = 32
_MAX_OPENAI_TOOL_ARGUMENT_BYTES = 256 * 1024
_MAX_OPENAI_TOOL_ARGUMENT_NESTING = 64
_MAX_OPENAI_CONTINUATION_BYTES = 4 * 1024 * 1024
_MAX_ANTHROPIC_TOOL_CALLS = 32
_MAX_ANTHROPIC_TOOL_ARGUMENT_BYTES = 256 * 1024
_MAX_ANTHROPIC_TOOL_ARGUMENT_NESTING = 64


@dataclass(slots=True)
class _OpenAIFunctionCall:
    output_index: int
    item_id: str
    call_id: str
    name: str
    argument_parts: list[str] = field(default_factory=list)
    argument_bytes: int = 0
    final_arguments: str | None = None
    arguments_done: bool = False
    item_done: bool = False

    def append_arguments(self, delta: str) -> None:
        if self.arguments_done or self.item_done:
            raise _openai_invalid_stream()
        try:
            self.argument_bytes += len(delta.encode("utf-8"))
        except UnicodeEncodeError:
            raise _openai_invalid_stream() from None
        if self.argument_bytes > _MAX_OPENAI_TOOL_ARGUMENT_BYTES:
            raise _openai_invalid_stream()
        self.argument_parts.append(delta)

    def set_final_arguments(self, arguments: str, *, from_item: bool) -> None:
        try:
            argument_bytes = len(arguments.encode("utf-8"))
        except UnicodeEncodeError:
            raise _openai_invalid_stream() from None
        if argument_bytes > _MAX_OPENAI_TOOL_ARGUMENT_BYTES:
            raise _openai_invalid_stream()
        if from_item:
            if self.item_done:
                raise _openai_invalid_stream()
            self.item_done = True
        else:
            if self.arguments_done:
                raise _openai_invalid_stream()
            self.arguments_done = True
        if self.argument_parts and "".join(self.argument_parts) != arguments:
            raise _openai_invalid_stream()
        if self.final_arguments is not None and self.final_arguments != arguments:
            raise _openai_invalid_stream()
        self.final_arguments = arguments


def _openai_invalid_stream() -> ProviderError:
    return ProviderError(
        "provider_invalid_response",
        "OpenAI returned a stream Trellis could not read.",
    )


def _anthropic_invalid_stream() -> ProviderError:
    return ProviderError(
        "provider_invalid_response",
        "Anthropic returned a stream Trellis could not read.",
    )


@dataclass(slots=True)
class _AnthropicToolUse:
    index: int
    call_id: str
    name: str
    initial_input: dict[str, object]
    argument_parts: list[str] = field(default_factory=list)
    argument_bytes: int = 0
    stopped: bool = False
    arguments: dict[str, object] | None = None

    def append_arguments(self, delta: str) -> None:
        if self.stopped:
            raise _anthropic_invalid_stream()
        try:
            self.argument_bytes += len(delta.encode("utf-8"))
        except UnicodeEncodeError:
            raise _anthropic_invalid_stream() from None
        if self.argument_bytes > _MAX_ANTHROPIC_TOOL_ARGUMENT_BYTES:
            raise _anthropic_invalid_stream()
        self.argument_parts.append(delta)

    def finish(self) -> None:
        if self.stopped:
            raise _anthropic_invalid_stream()
        self.stopped = True
        if self.argument_parts and any(self.argument_parts):
            if self.initial_input:
                raise _anthropic_invalid_stream()
            argument_json = "".join(self.argument_parts)
        else:
            try:
                argument_json = json.dumps(self.initial_input, allow_nan=False)
            except TypeError, ValueError, RecursionError:
                raise _anthropic_invalid_stream() from None
        try:
            if len(argument_json.encode("utf-8")) > _MAX_ANTHROPIC_TOOL_ARGUMENT_BYTES:
                raise _anthropic_invalid_stream()
        except UnicodeEncodeError:
            raise _anthropic_invalid_stream() from None
        depth = 0
        in_string = False
        escaped = False
        for character in argument_json:
            if escaped:
                escaped = False
            elif in_string and character == "\\":
                escaped = True
            elif character == '"':
                in_string = not in_string
            elif not in_string and character in "[{":
                depth += 1
                if depth > _MAX_ANTHROPIC_TOOL_ARGUMENT_NESTING:
                    raise _anthropic_invalid_stream()
            elif not in_string and character in "]}":
                depth -= 1
                if depth < 0:
                    raise _anthropic_invalid_stream()
        try:
            arguments = json.loads(argument_json, parse_constant=_reject_json_constant)
        except json.JSONDecodeError, ValueError, RecursionError:
            raise _anthropic_invalid_stream() from None
        if not isinstance(arguments, dict):
            raise _anthropic_invalid_stream()
        self.arguments = arguments


def _anthropic_messages(messages: Sequence[ModelMessage]) -> list[dict[str, object]]:
    translated: list[dict[str, object]] = []
    previous_was_tool = False
    for item in messages:
        if item.role == "tool":
            if not item.tool_call_id:
                raise _anthropic_invalid_stream()
            block = {
                "type": "tool_result",
                "tool_use_id": item.tool_call_id,
                "content": item.content,
            }
            if previous_was_tool:
                content = translated[-1]["content"]
                if not isinstance(content, list):
                    raise _anthropic_invalid_stream()
                content.append(block)
            else:
                translated.append({"role": "user", "content": [block]})
            previous_was_tool = True
            continue
        previous_was_tool = False
        if item.role == "assistant" and item.tool_calls:
            blocks: list[dict[str, object]] = []
            if item.content:
                blocks.append({"type": "text", "text": item.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in item.tool_calls
            )
            translated.append({"role": "assistant", "content": blocks})
        else:
            translated.append({"role": item.role, "content": item.content})
    return translated


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _openai_strict_schema(schema: dict[str, object]) -> bool:
    """Use strict mode only for the small schema subset we can verify."""
    return schema.get("type") == "object" and _openai_strict_schema_node(schema)


def _openai_strict_schema_node(schema: object) -> bool:
    if not isinstance(schema, dict) or any(
        key
        not in {
            "type",
            "description",
            "enum",
            "const",
            "properties",
            "required",
            "additionalProperties",
            "items",
        }
        for key in schema
    ):
        return False
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        return (
            isinstance(properties, dict)
            and all(isinstance(key, str) for key in properties)
            and isinstance(required, list)
            and all(isinstance(key, str) for key in required)
            and len(required) == len(properties)
            and set(required) == set(properties)
            and schema.get("additionalProperties") is False
            and all(_openai_strict_schema_node(child) for child in properties.values())
        )
    if schema_type == "array":
        return _openai_strict_schema_node(schema.get("items"))
    if isinstance(schema_type, str):
        return schema_type in {"string", "number", "integer", "boolean", "null"}
    if isinstance(schema_type, list):
        return bool(schema_type) and all(
            isinstance(item, str) and item in {"string", "number", "integer", "boolean", "null"}
            for item in schema_type
        )
    return False


def _openai_reasoning_item(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        raise _openai_invalid_stream()
    if (
        not isinstance(item.get("id"), str)
        or not item["id"]
        or not isinstance(item.get("encrypted_content"), str)
        or not item["encrypted_content"]
        or item.get("status", "completed") != "completed"
    ):
        raise _openai_invalid_stream()
    return item


def _openai_reasoning_json(item: dict[str, object]) -> str:
    try:
        payload_json = json.dumps(item, separators=(",", ":"), allow_nan=False)
        if len(payload_json.encode("utf-8")) > _MAX_OPENAI_CONTINUATION_BYTES:
            raise _openai_invalid_stream()
    except TypeError, ValueError, RecursionError, UnicodeEncodeError:
        raise _openai_invalid_stream() from None
    return payload_json


def _openai_replay_reasoning(continuation: ModelContinuationItem) -> dict[str, object]:
    try:
        payload_bytes = len(continuation.payload_json.encode("utf-8"))
    except UnicodeEncodeError:
        raise _openai_invalid_stream() from None
    if payload_bytes > _MAX_OPENAI_CONTINUATION_BYTES:
        raise _openai_invalid_stream()
    try:
        item = json.loads(continuation.payload_json, parse_constant=_reject_json_constant)
    except json.JSONDecodeError, ValueError, RecursionError:
        raise _openai_invalid_stream() from None
    return _openai_reasoning_item(item)


def _openai_function_call(
    payload: dict[str, Any], calls: dict[str, _OpenAIFunctionCall]
) -> _OpenAIFunctionCall:
    item_id = payload.get("item_id")
    output_index = payload.get("output_index")
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(output_index, int)
        or isinstance(output_index, bool)
        or output_index < 0
    ):
        raise _openai_invalid_stream()
    call = calls.get(item_id)
    if call is None or call.output_index != output_index:
        raise _openai_invalid_stream()
    return call


def _openai_completed_calls(calls: dict[str, _OpenAIFunctionCall]) -> list[ModelToolCall]:
    completed: list[tuple[int, ModelToolCall]] = []
    for call in calls.values():
        arguments = call.final_arguments
        if arguments is None:
            raise _openai_invalid_stream()
        depth = 0
        in_string = False
        escaped = False
        for character in arguments:
            if escaped:
                escaped = False
            elif in_string and character == "\\":
                escaped = True
            elif character == '"':
                in_string = not in_string
            elif not in_string and character in "[{":
                depth += 1
                if depth > _MAX_OPENAI_TOOL_ARGUMENT_NESTING:
                    raise _openai_invalid_stream()
            elif not in_string and character in "]}":
                depth -= 1
                if depth < 0:
                    raise _openai_invalid_stream()
        try:
            parsed = json.loads(arguments, parse_constant=_reject_json_constant)
        except json.JSONDecodeError, ValueError, RecursionError:
            raise _openai_invalid_stream() from None
        if not isinstance(parsed, dict):
            raise _openai_invalid_stream()
        completed.append(
            (call.output_index, ModelToolCall(id=call.call_id, name=call.name, arguments=parsed))
        )
    completed.sort(key=lambda item: item[0])
    return [call for _, call in completed]


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
    input_items: list[dict[str, object]] = []
    for message in request.messages:
        if message.role == "tool":
            if not message.tool_call_id:
                raise _openai_invalid_stream()
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        for continuation in message.continuation_items:
            if (
                continuation.provider_id == request.provider_id
                and continuation.model_id == request.model_id
            ):
                input_items.append(_openai_replay_reasoning(continuation))
        if message.content or (not message.tool_calls and not message.continuation_items):
            input_items.append({"role": message.role, "content": message.content})
        for call in message.tool_calls:
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":"), allow_nan=False),
                }
            )

    body = {
        "model": request.upstream_model_id,
        "input": input_items,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "medium"},
        "max_output_tokens": request.max_output_tokens,
        "safety_identifier": user_id,
    }
    if request.system_instructions:
        body["instructions"] = request.system_instructions
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": _openai_strict_schema(tool.input_schema),
            }
            for tool in request.tools
        ]
    response_id: str | None = None
    saw_text = False
    saw_refusal_delta = False
    completed = False
    calls: dict[str, _OpenAIFunctionCall] = {}
    output_indexes: set[int] = set()
    call_ids: set[str] = set()
    reasoning_ids: dict[int, str] = {}
    reasoning_items: dict[int, ModelContinuationItem] = {}
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
        elif event_type == "response.output_item.added":
            item = _mapping(payload.get("item"))
            if item.get("type") == "reasoning":
                output_index = payload.get("output_index")
                item_id = item.get("id")
                if (
                    not isinstance(output_index, int)
                    or isinstance(output_index, bool)
                    or output_index < 0
                    or not isinstance(item_id, str)
                    or not item_id
                    or output_index in output_indexes
                ):
                    raise _openai_invalid_stream()
                reasoning_ids[output_index] = item_id
                output_indexes.add(output_index)
                continue
            if item.get("type") != "function_call":
                continue
            output_index = payload.get("output_index")
            item_id = item.get("id")
            call_id = item.get("call_id")
            name = item.get("name")
            initial_arguments = item.get("arguments", "")
            if (
                not isinstance(output_index, int)
                or isinstance(output_index, bool)
                or output_index < 0
                or not isinstance(item_id, str)
                or not item_id
                or not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(initial_arguments, str)
                or item_id in calls
                or call_id in call_ids
                or output_index in output_indexes
                or len(calls) >= _MAX_OPENAI_TOOL_CALLS
            ):
                raise _openai_invalid_stream()
            call = _OpenAIFunctionCall(output_index, item_id, call_id, name)
            if initial_arguments:
                call.append_arguments(initial_arguments)
            calls[item_id] = call
            call_ids.add(call_id)
            output_indexes.add(output_index)
        elif event_type == "response.function_call_arguments.delta":
            call = _openai_function_call(payload, calls)
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise _openai_invalid_stream()
            call.append_arguments(delta)
        elif event_type == "response.function_call_arguments.done":
            call = _openai_function_call(payload, calls)
            arguments = payload.get("arguments")
            if ("name" in payload and payload["name"] != call.name) or not isinstance(
                arguments, str
            ):
                raise _openai_invalid_stream()
            call.set_final_arguments(arguments, from_item=False)
        elif event_type == "response.output_item.done":
            item = _mapping(payload.get("item"))
            if item.get("type") == "reasoning":
                output_index = payload.get("output_index")
                item_id = item.get("id")
                if (
                    not isinstance(output_index, int)
                    or isinstance(output_index, bool)
                    or output_index < 0
                    or not isinstance(item_id, str)
                    or not item_id
                    or output_index in reasoning_items
                    or (output_index in reasoning_ids and reasoning_ids[output_index] != item_id)
                    or (output_index in output_indexes and output_index not in reasoning_ids)
                ):
                    raise _openai_invalid_stream()
                reasoning_item = _openai_reasoning_item(item)
                reasoning_items[output_index] = ModelContinuationItem(
                    provider_id=request.provider_id,
                    model_id=request.model_id,
                    payload_json=_openai_reasoning_json(reasoning_item),
                )
                output_indexes.add(output_index)
                continue
            if item.get("type") != "function_call":
                continue
            call = _openai_function_call(
                {"item_id": item.get("id"), "output_index": payload.get("output_index")},
                calls,
            )
            arguments = item.get("arguments")
            if (
                item.get("call_id") != call.call_id
                or item.get("name") != call.name
                or not isinstance(arguments, str)
                or item.get("status", "completed") != "completed"
            ):
                raise _openai_invalid_stream()
            call.set_final_arguments(arguments, from_item=True)
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
            if calls and event_type == "response.incomplete":
                raise _openai_invalid_stream()
            if reasoning_ids.keys() - reasoning_items.keys():
                raise _openai_invalid_stream()
            completed_calls = _openai_completed_calls(calls)
            if not saw_text and not completed_calls:
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
            if event_type == "response.completed":
                for index in sorted(reasoning_items):
                    yield ModelStreamEvent(
                        kind="continuation_item", continuation_item=reasoning_items[index]
                    )
            for tool_call in completed_calls:
                yield ModelStreamEvent(kind="tool_call", tool_call=tool_call)
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
        "messages": _anthropic_messages(request.messages),
    }
    if request.system_instructions:
        body["system"] = request.system_instructions
    if request.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ]
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cache_creation_tokens = 0
    finish_reason: str | None = None
    saw_text = False
    completed = False
    block_types: dict[int, str] = {}
    stopped_blocks: set[int] = set()
    tool_uses: dict[int, _AnthropicToolUse] = {}
    tool_ids: set[str] = set()
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
            cache_creation_tokens = _optional_integer(usage.get("cache_creation_input_tokens", 0))
        elif event_type == "content_block_start":
            index = payload.get("index")
            block = _mapping(payload.get("content_block"))
            block_type = block.get("type")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index in block_types
                or not isinstance(block_type, str)
            ):
                raise _anthropic_invalid_stream()
            block_types[index] = block_type
            if block_type == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                initial_input = block.get("input")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or call_id in tool_ids
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(initial_input, dict)
                    or len(tool_uses) >= _MAX_ANTHROPIC_TOOL_CALLS
                ):
                    raise _anthropic_invalid_stream()
                tool_uses[index] = _AnthropicToolUse(index, call_id, name, initial_input)
                tool_ids.add(call_id)
            elif block_type == "text":
                initial_text = block.get("text", "")
                if not isinstance(initial_text, str):
                    raise _anthropic_invalid_stream()
                if initial_text:
                    saw_text = True
                    yield ModelStreamEvent(kind="text_delta", text=initial_text)
        elif event_type == "content_block_delta":
            delta = _mapping(payload.get("delta"))
            delta_type = delta.get("type")
            index = payload.get("index")
            if index is not None and (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index not in block_types
                or index in stopped_blocks
            ):
                raise _anthropic_invalid_stream()
            if delta_type == "input_json_delta":
                partial_json = delta.get("partial_json")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index not in tool_uses
                    or not isinstance(partial_json, str)
                ):
                    raise _anthropic_invalid_stream()
                tool_uses[index].append_arguments(partial_json)
            elif delta_type == "text_delta":
                if index is not None and (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or block_types.get(index) != "text"
                ):
                    raise _anthropic_invalid_stream()
                text = delta.get("text")
                if not isinstance(text, str):
                    raise _anthropic_invalid_stream()
                saw_text = saw_text or bool(text)
                yield ModelStreamEvent(kind="text_delta", text=text)
        elif event_type == "content_block_stop":
            index = payload.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index not in block_types
                or index in stopped_blocks
            ):
                raise _anthropic_invalid_stream()
            if index in tool_uses:
                tool_uses[index].finish()
            stopped_blocks.add(index)
        elif event_type == "message_delta":
            delta = _mapping(payload.get("delta"))
            stop_reason = delta.get("stop_reason")
            finish_reason = stop_reason if isinstance(stop_reason, str) else finish_reason
            usage = _mapping(payload.get("usage"))
            output_tokens = _optional_integer(usage.get("output_tokens"))
            if "cache_creation_input_tokens" in usage:
                cache_creation_tokens = _optional_integer(usage["cache_creation_input_tokens"])
        elif event_type == "message_stop":
            if block_types.keys() - stopped_blocks or (tool_uses and finish_reason != "tool_use"):
                raise _anthropic_invalid_stream()
            if not saw_text and not tool_uses:
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
                    cache_creation_tokens=cache_creation_tokens,
                    provider_response_id=response_id,
                )
            completed = True
            await provider_events.aclose()
            for index in sorted(tool_uses):
                item = tool_uses[index]
                if item.arguments is None:
                    raise _anthropic_invalid_stream()
                yield ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall(
                        id=item.call_id,
                        name=item.name,
                        arguments=item.arguments,
                    ),
                )
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
