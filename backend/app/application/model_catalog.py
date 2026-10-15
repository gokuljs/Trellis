"""Verified, immutable model metadata; no provider requests or SDK dependencies.

Prices describe first-party API token charges, before taxes or negotiated
discounts. A profile is metadata-ready only with verified limits and complete
Standard pricing. Credentials and adapter readiness remain caller concerns.
"""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

ServiceTier = Literal["standard", "fast", "batch", "flex", "ultrafast"]


@dataclass(frozen=True, slots=True)
class PriceRates:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    cache_creation_per_million: float | None = None
    cache_creation_1h_per_million: float | None = None


@dataclass(frozen=True, slots=True)
class PriceTier:
    service_tier: ServiceTier
    max_input_tokens: int | None
    rates: PriceRates


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    provider_id: str
    upstream_model_id: str
    name: str
    context_window_tokens: int | None
    max_output_tokens: int | None
    price_tiers: tuple[PriceTier, ...]
    sources: tuple[str, ...]
    as_of: str
    max_input_tokens: int | None = None
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_native_compaction: bool | None = None
    cache_tokens_included_in_input: bool = True
    long_context_pricing_scope: Literal["request", "session"] = "request"

    @property
    def pending_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not (
            _positive_integer(self.context_window_tokens)
            and _positive_integer(self.max_output_tokens)
            and (self.max_input_tokens is None or _positive_integer(self.max_input_tokens))
        ):
            reasons.append("limits_unverified")
        valid_rates = all(
            isfinite(rate) and rate >= 0
            for tier in self.price_tiers
            for rate in (
                tier.rates.input_per_million,
                tier.rates.cached_input_per_million,
                tier.rates.output_per_million,
                tier.rates.cache_creation_per_million,
                tier.rates.cache_creation_1h_per_million,
            )
            if rate is not None
        )
        if not valid_rates or not any(
            tier.service_tier == "standard" and tier.max_input_tokens is None
            for tier in self.price_tiers
        ):
            reasons.append("pricing_unverified")
        try:
            date.fromisoformat(self.as_of)
            dated = True
        except ValueError:
            dated = False
        source_prefixes = {
            "openai": ("https://developers.openai.com/", "https://platform.openai.com/"),
            "anthropic": ("https://platform.claude.com/",),
        }.get(self.provider_id, ())
        if (
            not dated
            or not self.sources
            or not all(source.startswith(source_prefixes) for source in self.sources)
        ):
            reasons.append("sources_unverified")
        return tuple(reasons)

    @property
    def metadata_ready(self) -> bool:
        return not self.pending_reasons


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    usable_input_tokens: int
    compaction_threshold_tokens: int

    def should_compact(self, estimated_input_tokens: int) -> bool:
        _check_token_count(estimated_input_tokens, "input tokens")
        return estimated_input_tokens >= self.compaction_threshold_tokens

    def fits(self, estimated_input_tokens: int) -> bool:
        _check_token_count(estimated_input_tokens, "input tokens")
        return estimated_input_tokens <= self.usable_input_tokens


def context_budget(profile: ModelProfile, *, output_reserve: int = 4096) -> ContextBudget | None:
    """Reserve output, 2% (at least 2048) safety space and any provider input cap."""
    context = profile.context_window_tokens
    maximum_output = profile.max_output_tokens
    if not _positive_integer(output_reserve):
        raise ValueError("output reserve must be a positive integer")
    if (
        context is None
        or maximum_output is None
        or not _positive_integer(context)
        or not _positive_integer(maximum_output)
        or (
            profile.max_input_tokens is not None and not _positive_integer(profile.max_input_tokens)
        )
    ):
        return None
    reserve = min(output_reserve, 4096, maximum_output)
    margin = max(2048, (context + 49) // 50)
    input_cap = min(context - reserve, profile.max_input_tokens or context)
    usable = input_cap - margin
    if usable <= 0:
        return None
    return ContextBudget(context, reserve, margin, usable, usable * 4 // 5)


@dataclass(frozen=True, slots=True)
class UsageTokens:
    """Provider input counts, not a universal total-input count.

    OpenAI includes cache reads and writes in input_tokens. Anthropic excludes
    both. cache_creation_tokens is the total across TTLs; the one-hour field is
    its subset. Default zero means the caller knows no such usage occurred;
    pass None for an unavailable count. Reasoning is already in output_tokens.
    """

    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None = 0
    cache_creation_tokens: int | None = 0
    cache_creation_1h_tokens: int | None = 0
    reasoning_tokens: int | None = None


def _check_token_count(value: int, name: str = "token counts") -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-negative integers")


def _usage_counts(profile: ModelProfile, usage: UsageTokens) -> tuple[int, int, int, int] | None:
    for count in (
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.cache_creation_tokens,
        usage.cache_creation_1h_tokens,
        usage.reasoning_tokens,
    ):
        if count is not None:
            _check_token_count(count)
    input_count = usage.input_tokens
    output_count = usage.output_tokens
    cached = usage.cached_input_tokens
    created = usage.cache_creation_tokens
    if input_count is None or output_count is None or cached is None or created is None:
        return None
    if profile.cache_tokens_included_in_input and cached + created > input_count:
        raise ValueError("cache tokens exceed input tokens")
    if usage.cache_creation_1h_tokens is not None and usage.cache_creation_1h_tokens > created:
        raise ValueError("one-hour cache tokens exceed total cache creation tokens")
    if usage.reasoning_tokens is not None and usage.reasoning_tokens > output_count:
        raise ValueError("reasoning tokens exceed output tokens")
    return input_count, output_count, cached, created


def total_usage_tokens(profile: ModelProfile, usage: UsageTokens) -> int | None:
    """Count cache and reasoning tokens once, using that provider's usage semantics."""
    counts = _usage_counts(profile, usage)
    if counts is None:
        return None
    input_count, output_count, cached, created = counts
    return (
        input_count
        + output_count
        + (0 if profile.cache_tokens_included_in_input else cached + created)
    )


def estimate_profile_cost(
    profile: ModelProfile,
    usage: UsageTokens,
    *,
    service_tier: str = "standard",
    pricing_context_tokens: int | None = None,
) -> float | None:
    """Return token cost or None when a required count/rate is unavailable.

    Use the served tier returned by the provider; 'auto' does not identify a
    price. GPT-5.5 callers must retain the session's highest pricing context
    and pass it as pricing_context_tokens. No reasoning charge is added to the
    already-inclusive output total. Provider tools and regional premiums are
    not token charges represented by this estimate.
    """
    counts = _usage_counts(profile, usage)
    if pricing_context_tokens is not None:
        _check_token_count(pricing_context_tokens, "pricing context tokens")
    if counts is None or not profile.metadata_ready:
        return None
    input_count, output_count, cached, created = counts
    ordinary = (
        input_count - cached - created if profile.cache_tokens_included_in_input else input_count
    )
    prompt_tokens = (
        input_count if profile.cache_tokens_included_in_input else input_count + cached + created
    )
    priced_context = max(prompt_tokens, pricing_context_tokens or 0)
    tier_name = {"default": "standard", "priority": "fast"}.get(service_tier, service_tier)
    tiers = sorted(
        (tier for tier in profile.price_tiers if tier.service_tier == tier_name),
        key=lambda tier: (
            tier.max_input_tokens if tier.max_input_tokens is not None else float("inf")
        ),
    )
    tier = next(
        (
            item
            for item in tiers
            if item.max_input_tokens is None or priced_context <= item.max_input_tokens
        ),
        None,
    )
    if tier is None:
        return None
    rates = tier.rates
    one_hour = usage.cache_creation_1h_tokens
    if created and (rates.cache_creation_per_million is None or one_hour is None):
        return None
    if one_hour and rates.cache_creation_1h_per_million is None:
        return None
    one_hour = one_hour or 0
    return (
        ordinary * rates.input_per_million
        + cached * rates.cached_input_per_million
        + (created - one_hour) * (rates.cache_creation_per_million or 0)
        + one_hour * (rates.cache_creation_1h_per_million or 0)
        + output_count * rates.output_per_million
    ) / 1_000_000


_AS_OF = "2026-10-15"
_OPENAI_PRICING = "https://developers.openai.com/api/docs/pricing"
_CLAUDE_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI_CACHE = "https://developers.openai.com/api/docs/guides/prompt-caching"
_OPENAI_COMPACTION = "https://developers.openai.com/api/docs/guides/latest-model"
_CLAUDE_CACHE = "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
_CLAUDE_COMPACTION = "https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand"


def _scaled(rates: PriceRates, multiplier: float) -> PriceRates:
    return PriceRates(
        rates.input_per_million * multiplier,
        rates.cached_input_per_million * multiplier,
        rates.output_per_million * multiplier,
        rates.cache_creation_per_million * multiplier
        if rates.cache_creation_per_million is not None
        else None,
        rates.cache_creation_1h_per_million * multiplier
        if rates.cache_creation_1h_per_million is not None
        else None,
    )


def _openai_six_profile(upstream_id: str, name: str, short: PriceRates) -> ModelProfile:
    long = PriceRates(
        short.input_per_million * 2,
        short.cached_input_per_million * 2,
        short.output_per_million * 1.5,
        short.cache_creation_per_million * 2
        if short.cache_creation_per_million is not None
        else None,
    )
    tiers = tuple(
        PriceTier(service, cap, _scaled(rates, multiplier))
        for service, multiplier in (("standard", 1), ("fast", 2), ("batch", 0.5), ("flex", 0.5))
        for cap, rates in ((272_000, short), (None, long))
    )
    if upstream_id in {"gpt-6-astra", "gpt-6.1-sol"}:
        tiers += tuple(
            PriceTier("ultrafast", cap, _scaled(rates, 6))
            for cap, rates in ((272_000, short), (None, long))
        )
    return ModelProfile(
        f"openai:{upstream_id}",
        "openai",
        upstream_id,
        name,
        1_050_000,
        128_000,
        tiers,
        (
            f"https://developers.openai.com/api/docs/models/{upstream_id}",
            _OPENAI_PRICING,
            _OPENAI_CACHE,
            _OPENAI_COMPACTION,
        ),
        _AS_OF,
        max_input_tokens=922_000,
        supports_native_compaction=True,
    )


def _claude_profile(
    upstream_id: str,
    name: str,
    page: str,
    short: PriceRates,
    *,
    long: PriceRates | None = None,
    fast: bool = False,
) -> ModelProfile:
    tiers = tuple(
        PriceTier(service, cap, _scaled(rates, multiplier))
        for service, multiplier in (("standard", 1), ("batch", 0.5))
        for cap, rates in (
            ((100_000, short), (None, long)) if long is not None else ((None, short),)
        )
    )
    if fast:
        tiers += (PriceTier("fast", None, _scaled(short, 2)),)
    return ModelProfile(
        f"anthropic:{upstream_id}",
        "anthropic",
        upstream_id,
        name,
        1_000_000,
        128_000,
        tiers,
        (
            f"https://platform.claude.com/docs/en/models/{page}/overview",
            _CLAUDE_PRICING,
            _CLAUDE_CACHE,
            _CLAUDE_COMPACTION,
        ),
        _AS_OF,
        supports_native_compaction=True,
        cache_tokens_included_in_input=False,
    )


_PROFILES = (
    ModelProfile(
        "openai:gpt-5.5",
        "openai",
        "gpt-5.5",
        "GPT-5.5",
        1_050_000,
        128_000,
        (
            PriceTier("standard", 272_000, PriceRates(5, 0.5, 30)),
            PriceTier("standard", None, PriceRates(10, 1, 45)),
            PriceTier("batch", 272_000, PriceRates(2.5, 0.25, 15)),
            PriceTier("batch", None, PriceRates(5, 0.5, 22.5)),
            PriceTier("flex", 272_000, PriceRates(2.5, 0.25, 15)),
            PriceTier("flex", None, PriceRates(5, 0.5, 22.5)),
            PriceTier("fast", 272_000, PriceRates(12.5, 1.25, 75)),
        ),
        (
            "https://developers.openai.com/api/docs/models/gpt-5.5",
            _OPENAI_PRICING,
            _OPENAI_CACHE,
            "https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.5",
        ),
        _AS_OF,
        supports_native_compaction=True,
        long_context_pricing_scope="session",
    ),
    _openai_six_profile("gpt-6-astra", "GPT-6 Astra", PriceRates(10, 1, 50, 12.5)),
    _openai_six_profile("gpt-6.1-sol", "GPT-6.1 Sol", PriceRates(2, 0.1, 10, 2.5)),
    _openai_six_profile("gpt-6-luna", "GPT-6 Luna", PriceRates(0.1, 0.01, 0.5, 0.125)),
    _claude_profile(
        "claude-sonnet-5", "Claude Sonnet 5", "sonnet-5", PriceRates(2, 0.2, 10, 2.5, 4)
    ),
    _claude_profile(
        "claude-sonnet-5-5", "Claude Sonnet 5.5", "sonnet-5-5", PriceRates(2, 0.1, 10, 2.5, 4)
    ),
    _claude_profile(
        "claude-opus-5-5", "Claude Opus 5.5", "opus-5-5", PriceRates(4, 0.2, 20, 5, 8), fast=True
    ),
    _claude_profile(
        "claude-haiku-5-5",
        "Claude Haiku 5.5",
        "haiku-5-5",
        PriceRates(0.1, 0.01, 0.5, 0.125, 0.2),
        long=PriceRates(0.5, 0.05, 2.5, 0.625, 1),
    ),
)


def profile_for_model(model_id: str) -> ModelProfile | None:
    """Exact lookup only: an unknown or future ID never borrows another model's limits."""
    return next((profile for profile in _PROFILES if profile.id == model_id), None)


def catalog_profiles() -> tuple[ModelProfile, ...]:
    """Return immutable profiles for joining with the repository's model descriptors."""
    return _PROFILES
