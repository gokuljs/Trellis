"""Bounded JSON snapshots for requests that may be dispatched after restart."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Literal, cast

from app.application.context_budget import input_fingerprint
from app.domain.context import ContextItem, ContextPreflight
from app.domain.runtime import (
    ModelContinuationItem,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolSpec,
    RunSnapshot,
)

MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


def bounded_snapshot(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("execution snapshot must contain finite JSON") from error
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("execution snapshot exceeds its size limit")


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("execution snapshot requires an object")
    return cast(dict[str, object], value)


def _text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or "\0" in value:
        raise ValueError("invalid execution snapshot text")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid execution snapshot token count")
    return value


def _array(value: object, limit: int = 1024) -> list[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid execution snapshot list")
    return cast(list[object], value)


def _message(value: object, run: RunSnapshot) -> ModelMessage:
    raw = _object(value)
    role = raw.get("role")
    if role not in {"user", "assistant", "tool"}:
        raise ValueError("invalid execution snapshot message role")
    tool_id = raw.get("tool_call_id")
    if tool_id is not None:
        tool_id = _text(tool_id)
    calls: list[ModelToolCall] = []
    for value in _array(raw.get("tool_calls", []), 100):
        call = _object(value)
        calls.append(
            ModelToolCall(
                _text(call.get("id")),
                _text(call.get("name")),
                dict(_object(call.get("arguments"))),
            )
        )
    continuation: list[ModelContinuationItem] = []
    for value in _array(raw.get("continuation_items", []), 100):
        item = _object(value)
        if (item.get("provider_id"), item.get("model_id")) != (run.provider_id, run.model_id):
            raise ValueError("execution continuation belongs to another model")
        continuation.append(
            ModelContinuationItem(run.provider_id, run.model_id, _text(item.get("payload_json")))
        )
    if (role == "tool") != (tool_id is not None) or (role != "assistant" and calls):
        raise ValueError("invalid execution snapshot tool exchange")
    return ModelMessage(
        cast(Literal["user", "assistant", "tool"], role),
        _text(raw.get("content"), empty=True),
        tool_id,
        tuple(calls),
        tuple(continuation),
    )


def request_snapshot(request: ModelRequest) -> dict[str, object]:
    result = asdict(request)
    # JSON arrays make this representation identical before and after storage.
    result = cast(dict[str, object], json.loads(json.dumps(result, allow_nan=False)))
    bounded_snapshot(result)
    return result


def restore_model_request(snapshot: Mapping[str, object], run: RunSnapshot) -> ModelRequest:
    bounded_snapshot(snapshot)
    if tuple(
        snapshot.get(key)
        for key in ("provider_id", "model_id", "adapter_kind", "upstream_model_id")
    ) != (run.provider_id, run.model_id, run.adapter_kind, run.upstream_model_id):
        raise ValueError("execution request belongs to another model")
    tools: list[ModelToolSpec] = []
    for value in _array(snapshot.get("tools", []), 100):
        item = _object(value)
        tools.append(
            ModelToolSpec(
                _text(item.get("name")),
                _text(item.get("description"), empty=True),
                dict(_object(item.get("input_schema"))),
            )
        )
    output = _integer(snapshot.get("max_output_tokens"))
    if output == 0:
        raise ValueError("execution output limit must be positive")
    native = snapshot.get("native_context_json")
    return ModelRequest(
        run.provider_id,
        run.model_id,
        run.adapter_kind,
        run.upstream_model_id,
        tuple(_message(item, run) for item in _array(snapshot.get("messages"))),
        output,
        _text(snapshot.get("system_instructions", ""), empty=True),
        tuple(tools),
        _text(native) if native is not None else None,
    )


def plan_snapshot(plan: ContextPreflight) -> dict[str, object]:
    result = {
        **plan.snapshot(),
        "request": request_snapshot(plan.request),
        "items": [asdict(item) for item in plan.sources],
        "prefix_length": len(plan.compactable_prefix),
    }
    result = cast(dict[str, object], json.loads(json.dumps(result, allow_nan=False)))
    bounded_snapshot(result)
    return result


def restore_context_plan(value: object, run: RunSnapshot) -> ContextPreflight:
    bounded_snapshot(value)
    raw = _object(value)
    request = restore_model_request(_object(raw.get("request")), run)
    items: list[ContextItem] = []
    for value in _array(raw.get("items"), 512):
        item = _object(value)
        kind = item.get("source_kind")
        required = item.get("required")
        if kind not in {"message", "run_message", "checkpoint"} or not isinstance(required, bool):
            raise ValueError("invalid execution context source")
        items.append(
            ContextItem(
                _text(item.get("source_id")),
                _text(item.get("turn_id")),
                _message(item.get("message"), run),
                required,
                cast(Literal["message", "run_message", "checkpoint"], kind),
            )
        )
    prefix = _integer(raw.get("prefix_length"))
    if (
        prefix > len(items)
        or tuple(item.message for item in items) != request.messages
        or raw.get("input_hash") != input_fingerprint(request)
        or raw.get("source_hash")
        != hashlib.sha256(
            json.dumps(
                [asdict(item) for item in items],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    ):
        raise ValueError("execution context does not match its saved request")
    flags = (raw.get("fits"), raw.get("needs_compaction"))
    if any(not isinstance(flag, bool) for flag in flags):
        raise ValueError("invalid execution context flags")
    cost = raw.get("estimated_call_cost_usd")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0
    ):
        raise ValueError("invalid execution context cost")
    checkpoint = raw.get("checkpoint_id")
    return ContextPreflight(
        request,
        tuple(items),
        tuple(items[:prefix]),
        tuple(items[prefix:]),
        _integer(raw.get("input_tokens")),
        _text(raw.get("count_method")),
        _text(raw.get("input_hash")),
        _integer(raw.get("output_reserve_tokens")),
        _integer(raw.get("safety_margin_tokens")),
        _integer(raw.get("usable_input_tokens")),
        cast(bool, flags[0]),
        cast(bool, flags[1]),
        _text(checkpoint) if checkpoint is not None else None,
        float(cost) if cost is not None else None,
        _text(raw.get("source_hash")),
        _integer(raw.get("pricing_context_tokens")),
    )
