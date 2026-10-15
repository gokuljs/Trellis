import importlib
from dataclasses import FrozenInstanceError, replace
from types import ModuleType

import pytest


def catalog() -> ModuleType:
    try:
        return importlib.import_module("app.application.model_catalog")
    except ModuleNotFoundError:
        pytest.fail("verified model catalog is not implemented")


@pytest.mark.parametrize(
    ("model_id", "usable", "margin", "threshold"),
    [
        ("openai:gpt-5.5", 1_024_904, 21_000, 819_923),
        ("anthropic:claude-sonnet-5", 975_904, 20_000, 780_723),
        ("openai:gpt-6.1-sol", 901_000, 21_000, 720_800),
    ],
)
def test_known_context_budget_reserves_output_and_respects_separate_input_cap(
    model_id: str, usable: int, margin: int, threshold: int
) -> None:
    module = catalog()
    profile = module.profile_for_model(model_id)
    assert profile is not None
    assert profile.metadata_ready
    budget = module.context_budget(profile)
    assert budget.usable_input_tokens == usable
    assert budget.output_reserve_tokens == 4096
    assert budget.safety_margin_tokens == margin
    assert budget.compaction_threshold_tokens == threshold
    assert not budget.should_compact(threshold - 1)
    assert budget.should_compact(threshold)
    assert budget.fits(usable)
    assert not budget.fits(usable + 1)


@pytest.mark.parametrize(
    ("model_id", "input_tokens", "output_tokens", "cached", "created", "one_hour", "cost", "total"),
    [
        ("openai:gpt-5.5", 1000, 100, 200, 0, 0, 0.0071, 1100),
        ("openai:gpt-6.1-sol", 1000, 100, 200, 300, 0, 0.00277, 1100),
        ("anthropic:claude-sonnet-5", 1000, 100, 200, 300, 100, 0.00394, 1600),
        ("openai:gpt-6-astra", 1000, 100, 0, 0, 0, 0.015, 1100),
        ("openai:gpt-6-luna", 1000, 100, 0, 0, 0, 0.00015, 1100),
        ("anthropic:claude-sonnet-5-5", 1000, 100, 200, 300, 100, 0.00392, 1600),
        ("anthropic:claude-opus-5-5", 1000, 100, 200, 300, 100, 0.00784, 1600),
        ("anthropic:claude-haiku-5-5", 1000, 100, 200, 300, 100, 0.000197, 1600),
    ],
)
def test_cost_uses_each_models_cache_semantics_and_does_not_double_count_reasoning(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cached: int,
    created: int,
    one_hour: int,
    cost: float,
    total: int,
) -> None:
    module = catalog()
    profile = module.profile_for_model(model_id)
    assert profile is not None
    usage = module.UsageTokens(input_tokens, output_tokens, cached, created, one_hour, 80)
    assert module.estimate_profile_cost(profile, usage) == pytest.approx(cost)
    assert module.total_usage_tokens(profile, usage) == total


@pytest.mark.parametrize(
    ("model_id", "input_tokens", "cached", "created", "expected"),
    [
        ("openai:gpt-6.1-sol", 272_000, 0, 0, 0.5441),
        ("openai:gpt-6.1-sol", 272_001, 0, 0, 1.088154),
        ("openai:gpt-6.1-sol", 272_001, 200_000, 72_000, 0.400154),
        ("anthropic:claude-haiku-5-5", 50, 99_950, 0, 0.0010095),
        ("anthropic:claude-haiku-5-5", 50, 100_000, 0, 0.00505),
        ("anthropic:claude-haiku-5-5", 50, 99_900, 100, 0.0051075),
    ],
)
def test_prompt_price_tier_counts_all_input_even_when_cached(
    model_id: str, input_tokens: int, cached: int, created: int, expected: float
) -> None:
    module = catalog()
    profile = module.profile_for_model(model_id)
    assert profile is not None
    usage = module.UsageTokens(input_tokens, 10, cached, created)
    assert module.estimate_profile_cost(profile, usage) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("standard", 0.003),
        ("batch", 0.0015),
        ("flex", 0.0015),
        ("fast", 0.006),
        ("priority", 0.006),
        ("ultrafast", 0.018),
        ("default", 0.003),
    ],
)
def test_service_tier_rates_use_the_actual_served_tier(tier: str, expected: float) -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-6.1-sol")
    usage = module.UsageTokens(1000, 100)
    assert module.estimate_profile_cost(profile, usage, service_tier=tier) == pytest.approx(
        expected
    )


def test_session_long_context_rate_cannot_revert_after_a_smaller_gpt55_request() -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-5.5")
    usage = module.UsageTokens(1000, 100, 200)
    assert module.estimate_profile_cost(
        profile, usage, pricing_context_tokens=272_001
    ) == pytest.approx(0.0127)


def test_unverified_metadata_and_unknown_models_never_borrow_limits_or_rates() -> None:
    module = catalog()
    assert module.profile_for_model("openai:gpt-6.1-sol-unverified") is None
    profile = module.profile_for_model("openai:gpt-5.5")
    for missing in (
        {"context_window_tokens": None},
        {"max_output_tokens": None},
        {"price_tiers": ()},
        {"sources": ()},
        {"as_of": "unknown"},
    ):
        pending = replace(profile, **missing)
        assert not pending.metadata_ready
        assert pending.pending_reasons
    assert module.context_budget(replace(profile, context_window_tokens=None)) is None
    assert (
        module.estimate_profile_cost(replace(profile, price_tiers=()), module.UsageTokens(10, 5))
        is None
    )


@pytest.mark.parametrize("reserve", [0, -1, True])
def test_output_reserve_must_be_a_positive_integer(reserve: int) -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-5.5")
    with pytest.raises(ValueError, match="output reserve"):
        module.context_budget(profile, output_reserve=reserve)


def test_small_model_budget_caps_output_and_preserves_minimum_margin() -> None:
    module = catalog()
    profile = replace(
        module.profile_for_model("openai:gpt-5.5"),
        context_window_tokens=16_384,
        max_output_tokens=2048,
    )
    budget = module.context_budget(profile, output_reserve=100_000)
    assert budget.output_reserve_tokens == 2048
    assert budget.safety_margin_tokens == 2048
    assert budget.usable_input_tokens == 12_288
    assert budget.compaction_threshold_tokens == 9830
    with pytest.raises(ValueError, match="input tokens"):
        budget.fits(-1)
    with pytest.raises(ValueError, match="input tokens"):
        budget.should_compact(True)
    assert (
        module.context_budget(replace(profile, context_window_tokens=4096, max_output_tokens=4096))
        is None
    )


def test_catalog_profiles_are_immutable() -> None:
    profile = catalog().profile_for_model("openai:gpt-5.5")
    with pytest.raises(FrozenInstanceError):
        profile.context_window_tokens = 999_999


def test_missing_usage_or_unpublished_service_pricing_does_not_invent_a_cost() -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-6.1-sol")
    for usage in (
        module.UsageTokens(None, 10),
        module.UsageTokens(10, None),
        module.UsageTokens(10, 10, None),
        module.UsageTokens(10, 10, 0, None),
    ):
        assert module.estimate_profile_cost(profile, usage) is None
        assert module.total_usage_tokens(profile, usage) is None
    assert (
        module.estimate_profile_cost(profile, module.UsageTokens(10, 10), service_tier="auto")
        is None
    )
    sonnet = module.profile_for_model("anthropic:claude-sonnet-5")
    assert (
        module.estimate_profile_cost(sonnet, module.UsageTokens(10, 10), service_tier="fast")
        is None
    )


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ((-1, 10, 0, 0, 0, None), "non-negative integers"),
        ((True, 10, 0, 0, 0, None), "non-negative integers"),
        ((10, 10, 8, 3, 0, None), "cache tokens exceed input"),
        ((10, 10, 0, 3, 4, None), "one-hour cache tokens exceed"),
        ((10, 10, 0, 0, 0, 11), "reasoning tokens exceed output"),
    ],
)
def test_invalid_usage_is_rejected_before_pricing(counts: tuple[object, ...], message: str) -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-6.1-sol")
    usage = module.UsageTokens(*counts)
    with pytest.raises(ValueError, match=message):
        module.estimate_profile_cost(profile, usage)


def test_verified_catalog_can_offer_native_compaction_for_each_known_model() -> None:
    module = catalog()
    profiles = module.catalog_profiles()
    assert isinstance(profiles, tuple)
    assert len({profile.id for profile in profiles}) == len(profiles)
    for profile in profiles:
        assert profile.metadata_ready
        assert profile.supports_native_compaction
        assert module.context_budget(profile) is not None
        assert module.estimate_profile_cost(profile, module.UsageTokens(1000, 100)) is not None


def test_missing_cache_write_rate_never_turns_writes_into_free_input() -> None:
    module = catalog()
    old_model = module.profile_for_model("openai:gpt-5.5")
    assert module.estimate_profile_cost(old_model, module.UsageTokens(1000, 100, 200, 300)) is None
    newer = module.profile_for_model("openai:gpt-6.1-sol")
    assert module.estimate_profile_cost(newer, module.UsageTokens(1000, 100, 200, 300, 100)) is None
    sonnet = module.profile_for_model("anthropic:claude-sonnet-5")
    usage = module.UsageTokens(1000, 100, 200, 300, None)
    assert module.estimate_profile_cost(sonnet, usage) is None
    assert module.total_usage_tokens(sonnet, usage) == 1600


def test_bad_profile_limits_or_nonfinite_rates_cannot_become_ready() -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-5.5")
    for missing in (
        {"context_window_tokens": -1},
        {"max_output_tokens": True},
        {"max_input_tokens": -1},
        {"sources": ("https://example.com/guessed",)},
    ):
        pending = replace(profile, **missing)
        assert not pending.metadata_ready
    rates = module.PriceRates(float("nan"), 0, 1)
    pending = replace(profile, price_tiers=(module.PriceTier("standard", None, rates),))
    assert not pending.metadata_ready


def test_unknown_context_limits_do_not_produce_a_budget() -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-5.5")
    for values in (
        {"context_window_tokens": 0},
        {"max_output_tokens": 0},
        {"max_input_tokens": -1},
    ):
        assert module.context_budget(replace(profile, **values)) is None


def test_negative_pricing_context_is_rejected() -> None:
    module = catalog()
    profile = module.profile_for_model("openai:gpt-5.5")
    with pytest.raises(ValueError, match="pricing context tokens"):
        module.estimate_profile_cost(
            profile, module.UsageTokens(1000, 100), pricing_context_tokens=-1
        )
