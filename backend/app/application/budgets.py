from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.application.model_catalog import (
    ModelProfile,
    UsageTokens,
    estimate_profile_cost,
    profile_for_model,
    total_usage_tokens,
)

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


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PricingUnavailable(Exception):
    def __init__(self, model_id: str) -> None:
        super().__init__(f"pricing unavailable for {model_id}")
        self.model_id = model_id


def limits_for_preset(preset: BudgetPreset, *, run_mode: str = "chat") -> RunLimits:
    if run_mode == "research":
        from app.domain.research import research_limits

        limits = research_limits(preset)
        return RunLimits(
            limits.model_calls,
            limits.tool_calls,
            limits.total_tokens,
            limits.seconds,
            limits.model_cost_usd,
        )
    return _PRESETS[preset]


def _profile(model_id: str) -> ModelProfile:
    profile = profile_for_model(model_id)
    if profile is None or not profile.metadata_ready:
        raise PricingUnavailable(model_id)
    return profile


def price_for_model(model_id: str) -> ModelPrice:
    """Compatibility view of standard short-context rates, never a pricing lookup."""
    profile = _profile(model_id)
    tier = next(item for item in profile.price_tiers if item.service_tier == "standard")
    rates = tier.rates
    return ModelPrice(
        rates.input_per_million,
        rates.cached_input_per_million,
        rates.output_per_million,
        rates.cache_creation_1h_per_million or rates.cache_creation_per_million or 0,
        profile.cache_tokens_included_in_input,
        profile.sources[0],
        profile.as_of,
    )


def _usage(
    profile: ModelProfile,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None,
    cache_creation_1h_tokens: int | None,
    reasoning_tokens: int | None,
) -> UsageTokens:
    if (
        profile.cache_tokens_included_in_input
        and input_tokens is not None
        and cached_tokens is not None
        and cached_tokens > input_tokens
    ):
        raise ValueError("cached tokens exceed input tokens")
    cached_tokens = cached_tokens or 0
    if cache_creation_tokens is None and all(
        tier.rates.cache_creation_per_million is None for tier in profile.price_tiers
    ):
        cache_creation_tokens = 0
    # Legacy callers do not identify a cache-write TTL. Retain the conservative
    # one-hour estimate; adapters with actual TTL counts can pass an explicit 0.
    one_hour = (
        cache_creation_tokens
        if cache_creation_1h_tokens is None
        and any(
            tier.rates.cache_creation_1h_per_million is not None for tier in profile.price_tiers
        )
        else cache_creation_1h_tokens or 0
    )
    return UsageTokens(
        input_tokens,
        output_tokens,
        cached_tokens,
        cache_creation_tokens,
        one_hour,
        reasoning_tokens,
    )


def estimate_model_cost(
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None = 0,
    *,
    cache_creation_1h_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    service_tier: str = "standard",
    pricing_context_tokens: int | None = None,
) -> float | None:
    profile = _profile(model_id)
    return estimate_profile_cost(
        profile,
        _usage(
            profile,
            input_tokens,
            output_tokens,
            cached_tokens,
            cache_creation_tokens,
            cache_creation_1h_tokens,
            reasoning_tokens,
        ),
        service_tier=service_tier,
        pricing_context_tokens=pricing_context_tokens,
    )


def count_total_tokens(
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_creation_tokens: int | None = 0,
) -> int | None:
    profile = _profile(model_id)
    return total_usage_tokens(
        profile,
        _usage(
            profile, input_tokens, output_tokens, cached_tokens, cache_creation_tokens, None, None
        ),
    )


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
