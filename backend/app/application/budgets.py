from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

BudgetPreset = Literal["conservative", "longer"]


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_model_calls: int
    max_tool_calls: int
    max_total_tokens: int
    max_seconds: int
    max_cost_usd: float


_PRESETS: dict[BudgetPreset, RunLimits] = {
    "conservative": RunLimits(8, 16, 100_000, 600, 2.0),
    "longer": RunLimits(15, 30, 200_000, 1200, 5.0),
}


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    cache_creation_per_million: float
    cached_input_included_in_input: bool
    source: str
    as_of: str


_PRICES: dict[str, ModelPrice] = {
    "openai:gpt-5.5": ModelPrice(
        5.0,
        0.5,
        30.0,
        0.0,
        True,
        "https://developers.openai.com/api/docs/models/gpt-5.5",
        "2026-10-07",
    ),
    "anthropic:claude-sonnet-5": ModelPrice(
        2.0,
        0.2,
        10.0,
        4.0,
        False,
        "https://platform.claude.com/docs/en/models/sonnet-5/overview",
        "2026-10-07",
    ),
}


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PricingUnavailable(Exception):
    def __init__(self, model_id: str) -> None:
        super().__init__(f"pricing unavailable for {model_id}")
        self.model_id = model_id


def limits_for_preset(preset: BudgetPreset) -> RunLimits:
    return _PRESETS[preset]


def price_for_model(model_id: str) -> ModelPrice:
    try:
        return _PRICES[model_id]
    except KeyError:
        raise PricingUnavailable(model_id) from None


def estimate_model_cost(
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None = 0,
) -> float | None:
    price = price_for_model(model_id)
    counts = _checked_counts(
        price, input_tokens, output_tokens, cached_tokens, cache_creation_tokens
    )
    if counts is None:
        return None
    input_count, output_count, cached, created = counts
    ordinary = input_count - cached if price.cached_input_included_in_input else input_count
    return (
        ordinary * price.input_per_million
        + cached * price.cached_input_per_million
        + created * price.cache_creation_per_million
        + output_count * price.output_per_million
    ) / 1_000_000


def count_total_tokens(
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None = 0,
) -> int | None:
    price = price_for_model(model_id)
    counts = _checked_counts(
        price, input_tokens, output_tokens, cached_tokens, cache_creation_tokens
    )
    if counts is None:
        return None
    input_count, output_count, cached, created = counts
    return (
        input_count
        + output_count
        + created
        + (0 if price.cached_input_included_in_input else cached)
    )


def _checked_counts(
    price: ModelPrice,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None,
) -> tuple[int, int, int, int] | None:
    if (
        input_tokens is None
        or output_tokens is None
        or (price.cache_creation_per_million > 0 and cache_creation_tokens is None)
    ):
        return None
    cached = cached_tokens or 0
    created = cache_creation_tokens or 0
    if min(input_tokens, output_tokens, cached, created) < 0:
        raise ValueError("token counts cannot be negative")
    if price.cached_input_included_in_input and cached > input_tokens:
        raise ValueError("cached tokens exceed input tokens")
    return input_tokens, output_tokens, cached, created


def check_step_budget(
    limits: RunLimits,
    *,
    kind: Literal["model", "tool"],
    model_calls: int,
    tool_calls: int,
    tokens: int,
    cost_usd: float,
    deadline: datetime,
) -> None:
    if kind == "model" and model_calls >= limits.max_model_calls:
        raise BudgetExceeded("model_call_limit")
    if kind == "tool" and tool_calls >= limits.max_tool_calls:
        raise BudgetExceeded("tool_call_limit")
    check_resource_budget(limits, tokens=tokens, cost_usd=cost_usd, deadline=deadline)


def check_resource_budget(
    limits: RunLimits,
    *,
    tokens: int,
    cost_usd: float,
    deadline: datetime,
) -> None:
    if tokens >= limits.max_total_tokens:
        raise BudgetExceeded("token_limit")
    if cost_usd >= limits.max_cost_usd:
        raise BudgetExceeded("cost_limit")
    if datetime.now(UTC) >= deadline:
        raise BudgetExceeded("time_limit")
