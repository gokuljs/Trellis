"""Budget complete context units without silently dropping the transcript.

The byte-based fallback is deliberately conservative and is always labelled as
an estimate. Provider counts are accepted only for the same normalized input.
An over-budget plan with an eligible prefix must be compacted and counted again
before a generation call. The current turn is never an eligible prefix.
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, replace

from app.application.errors import ApplicationError
from app.application.model_catalog import (
    ModelProfile,
    UsageTokens,
    context_budget,
    estimate_profile_cost,
)
from app.domain.context import ContextItem, ContextPreflight, InputTokenCount
from app.domain.runtime import ModelRequest

MAX_TOOL_RESULT_BYTES = 32768
MAX_RECENT_TURNS = 4
MAX_RECENT_TOKENS = 16384
_TRUNCATED = "[Tool result truncated for context.]"
_ENCODER = json.JSONEncoder(
    ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
)


def _input_record(request: ModelRequest) -> dict[str, object]:
    return {
        "provider_id": request.provider_id,
        "model_id": request.model_id,
        "adapter_kind": request.adapter_kind,
        "upstream_model_id": request.upstream_model_id,
        "system_instructions": request.system_instructions,
        "native_context_json": request.native_context_json,
        "messages": [asdict(message) for message in request.messages],
        "tools": [asdict(tool) for tool in request.tools],
    }


def _hash_record(record: object) -> str:
    digest = hashlib.sha256()
    for part in _ENCODER.iterencode(record):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def input_fingerprint(request: ModelRequest) -> str:
    return _hash_record(_input_record(request))


def estimate_input_tokens(request: ModelRequest) -> InputTokenCount:
    record = _input_record(request)
    # Protocol framing is included in addition to serialized text/schema bytes.
    tokens = sum(len(part.encode("utf-8")) for part in _ENCODER.iterencode(record))
    tokens += 16 * len(request.messages) + 32 * len(request.tools)
    return InputTokenCount(tokens, "utf8_bytes_estimate_v1", _hash_record(record))


def _normalize(items: Sequence[ContextItem]) -> tuple[ContextItem, ...]:
    normalized: list[ContextItem] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not item.source_id or not item.turn_id or (item.source_kind, item.source_id) in seen:
            raise ApplicationError(
                "context_invalid_sources", "Context source identities are invalid."
            )
        seen.add((item.source_kind, item.source_id))
        message = item.message
        if message.role == "tool":
            encoded = message.content.encode("utf-8")
            if len(encoded) > MAX_TOOL_RESULT_BYTES:
                limit = MAX_TOOL_RESULT_BYTES - len(_TRUNCATED.encode("utf-8"))
                message = replace(
                    message, content=encoded[:limit].decode("utf-8", errors="ignore") + _TRUNCATED
                )
        normalized.append(replace(item, message=message))
    return tuple(normalized)


def _turn_units(items: Sequence[ContextItem]) -> list[tuple[ContextItem, ...]]:
    units: list[list[ContextItem]] = []
    closed_turns: set[str] = set()
    pending: set[str] = set()
    tool_ids: set[str] = set()
    for item in items:
        if not units or units[-1][0].turn_id != item.turn_id:
            if pending or item.turn_id in closed_turns:
                raise ApplicationError(
                    "context_invalid_tool_sequence", "Context has an incomplete tool exchange."
                )
            if units:
                closed_turns.add(units[-1][0].turn_id)
            units.append([])
        message = item.message
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise ApplicationError(
                    "context_invalid_tool_sequence", "Context has an unmatched tool result."
                )
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ApplicationError(
                    "context_invalid_tool_sequence", "Context has an incomplete tool exchange."
                )
            for call in message.tool_calls:
                if message.role != "assistant" or call.id in tool_ids:
                    raise ApplicationError(
                        "context_invalid_tool_sequence", "Context has invalid tool requests."
                    )
                tool_ids.add(call.id)
                pending.add(call.id)
        units[-1].append(item)
    if pending:
        raise ApplicationError(
            "context_invalid_tool_sequence", "Context has an incomplete tool exchange."
        )
    return [tuple(unit) for unit in units]


def _tail_boundary(request: ModelRequest, units: Sequence[tuple[ContextItem, ...]]) -> int:
    first_required = min(
        (index for index, unit in enumerate(units) if any(item.required for item in unit)),
        default=len(units) - 1,
    )
    boundary = first_required
    tokens = 0
    for turns, index in enumerate(range(first_required - 1, -1, -1)):
        count = estimate_input_tokens(
            replace(
                request,
                messages=tuple(item.message for item in units[index]),
                system_instructions="",
                tools=(),
                native_context_json=None,
            )
        ).tokens
        if turns >= MAX_RECENT_TURNS or tokens + count > MAX_RECENT_TOKENS:
            break
        boundary = index
        tokens += count
    if all(item.source_kind == "checkpoint" for unit in units[:boundary] for item in unit):
        return 0
    return boundary


def _cost(
    profile: ModelProfile, input_tokens: int, output_tokens: int, pricing_context: int = 0
) -> float | None:
    # Reserve the highest applicable input rate before the provider reports
    # cache writes. Actual billing still uses the provider's usage breakdown.
    reserved_profile = replace(
        profile,
        price_tiers=tuple(
            replace(
                tier,
                rates=replace(
                    tier.rates,
                    input_per_million=max(
                        tier.rates.input_per_million,
                        tier.rates.cached_input_per_million,
                        tier.rates.cache_creation_per_million or 0,
                        tier.rates.cache_creation_1h_per_million or 0,
                    ),
                ),
            )
            for tier in profile.price_tiers
        ),
    )
    return estimate_profile_cost(
        reserved_profile,
        UsageTokens(input_tokens, output_tokens),
        pricing_context_tokens=pricing_context
        if profile.long_context_pricing_scope == "session"
        else None,
    )


def _affordable_output(
    profile: ModelProfile, input_tokens: int, target: int, limit: float, pricing_context: int = 0
) -> int:
    lower, upper = 0, target
    while lower < upper:
        middle = (lower + upper + 1) // 2
        amount = _cost(profile, input_tokens, middle, pricing_context)
        if amount is not None and amount <= limit:
            lower = middle
        else:
            upper = middle - 1
    return lower


def preflight_context(
    request: ModelRequest,
    *,
    items: Sequence[ContextItem],
    profile: ModelProfile,
    remaining_run_tokens: int,
    counted_input: InputTokenCount | None = None,
    checkpoint_id: str | None = None,
    remaining_run_cost_usd: float | None = None,
    pricing_context_tokens: int = 0,
) -> ContextPreflight:
    if request.native_context_json is not None and (
        counted_input is None or counted_input.method != "provider"
    ):
        raise ApplicationError(
            "native_context_count_required",
            "Native context requires a provider token count; use the portable summary instead.",
        )
    if not isinstance(remaining_run_tokens, int) or isinstance(remaining_run_tokens, bool):
        raise ValueError("remaining run tokens must be integers")
    if remaining_run_tokens <= 0:
        raise ApplicationError("run_budget_exhausted", "The run has no token budget remaining.")
    if not profile.metadata_ready or (
        profile.id,
        profile.provider_id,
        profile.upstream_model_id,
    ) != (request.model_id, request.provider_id, request.upstream_model_id):
        raise ApplicationError("model_not_available", "The selected model metadata is unavailable.")
    if remaining_run_cost_usd is not None and not math.isfinite(remaining_run_cost_usd):
        raise ValueError("remaining cost must be finite")
    normalized = _normalize(items)
    if not normalized or request.messages not in (
        tuple(item.message for item in items),
        tuple(item.message for item in normalized),
    ):
        raise ApplicationError(
            "context_invalid_sources", "The request does not match its context sources."
        )
    prepared = replace(request, messages=tuple(item.message for item in normalized))
    count = estimate_input_tokens(prepared) if counted_input is None else counted_input
    if count.tokens < 0 or isinstance(count.tokens, bool):
        raise ValueError("input token count must be a non-negative integer")
    if count.input_hash != input_fingerprint(prepared):
        raise ApplicationError(
            "context_count_mismatch", "The token count describes a different context."
        )
    units = _turn_units(normalized)
    required_units = [
        unit
        for index, unit in enumerate(units)
        if index == len(units) - 1 or any(item.required for item in unit)
    ]
    required_request = replace(
        prepared, messages=tuple(item.message for unit in required_units for item in unit)
    )
    required_tokens = (
        count.tokens
        if prepared.native_context_json is not None
        or required_request.messages == prepared.messages
        else min(estimate_input_tokens(required_request).tokens, count.tokens)
    )
    if required_tokens >= remaining_run_tokens:
        raise ApplicationError(
            "run_budget_exhausted", "The remaining run budget cannot fit the current input."
        )
    nominal = min(request.max_output_tokens, 4096, profile.max_output_tokens or 0)
    reserve = min(nominal, remaining_run_tokens - required_tokens)
    if reserve <= 0:
        raise ApplicationError("run_budget_exhausted", "The run has no output budget remaining.")
    if remaining_run_cost_usd is not None:
        floor = _cost(profile, required_tokens, 1, pricing_context_tokens)
        if floor is None or floor > remaining_run_cost_usd:
            raise ApplicationError(
                "budget_cost_limit", "The remaining run cost budget cannot fit the current input."
            )
        affordable = _affordable_output(
            profile, count.tokens, reserve, remaining_run_cost_usd, pricing_context_tokens
        )
        reserve = min(reserve, max(1, affordable))
    budget = context_budget(profile, output_reserve=reserve)
    if budget is None:
        raise ApplicationError(
            "model_not_available", "The selected model has no usable context budget."
        )
    usable = min(budget.usable_input_tokens, remaining_run_tokens - reserve)
    if required_tokens > usable:
        raise ApplicationError(
            "context_input_too_large",
            "The current request and tool exchanges exceed the context budget.",
        )
    boundary = _tail_boundary(prepared, units)
    prefix = tuple(item for unit in units[:boundary] for item in unit)
    tail = tuple(item for unit in units[boundary:] for item in unit)
    estimated_cost = _cost(profile, count.tokens, reserve, pricing_context_tokens)
    fits = count.tokens <= usable and (
        remaining_run_cost_usd is None
        or (estimated_cost is not None and estimated_cost <= remaining_run_cost_usd)
    )
    if not fits and not prefix:
        code = "budget_cost_limit" if count.tokens <= usable else "context_input_too_large"
        raise ApplicationError(
            code, "The current context cannot be shortened within the remaining budget."
        )
    source_hash = _hash_record([asdict(item) for item in normalized])
    return ContextPreflight(
        replace(prepared, max_output_tokens=reserve),
        normalized,
        prefix,
        tail,
        count.tokens,
        count.method,
        count.input_hash,
        reserve,
        budget.safety_margin_tokens,
        usable,
        fits,
        bool(prefix) and (not fits or count.tokens >= usable * 4 // 5),
        checkpoint_id,
        estimated_cost,
        source_hash,
        max(pricing_context_tokens, count.tokens),
    )
