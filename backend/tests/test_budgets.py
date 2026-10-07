from datetime import UTC, datetime, timedelta

import pytest

from app.application.budgets import (
    BudgetExceeded,
    PricingUnavailable,
    check_step_budget,
    count_total_tokens,
    estimate_model_cost,
    limits_for_preset,
)


def test_conservative_budget_stops_a_ninth_model_call() -> None:
    limits = limits_for_preset("conservative")
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    check_step_budget(
        limits, kind="model", model_calls=7, tool_calls=0, tokens=0, cost_usd=0, deadline=deadline
    )
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        check_step_budget(
            limits,
            kind="model",
            model_calls=8,
            tool_calls=0,
            tokens=0,
            cost_usd=0,
            deadline=deadline,
        )


def test_longer_budget_allows_more_steps_but_stops_at_its_tool_limit() -> None:
    limits = limits_for_preset("longer")
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    check_step_budget(
        limits,
        kind="tool",
        model_calls=8,
        tool_calls=16,
        tokens=100_000,
        cost_usd=2,
        deadline=deadline,
    )
    with pytest.raises(BudgetExceeded, match="tool_call_limit"):
        check_step_budget(
            limits,
            kind="tool",
            model_calls=8,
            tool_calls=30,
            tokens=100_000,
            cost_usd=2,
            deadline=deadline,
        )


def test_exhausting_tool_budget_still_allows_a_final_model_call() -> None:
    check_step_budget(
        limits_for_preset("conservative"),
        kind="model",
        model_calls=7,
        tool_calls=16,
        tokens=10_000,
        cost_usd=0.5,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    ("tokens", "cost_usd", "deadline", "reason"),
    [
        (100_000, 0.0, datetime.now(UTC) + timedelta(minutes=1), "token_limit"),
        (0, 2.0, datetime.now(UTC) + timedelta(minutes=1), "cost_limit"),
        (0, 0.0, datetime.now(UTC) - timedelta(seconds=1), "time_limit"),
    ],
)
def test_conservative_budget_stops_at_each_resource_ceiling(
    tokens: int, cost_usd: float, deadline: datetime, reason: str
) -> None:
    with pytest.raises(BudgetExceeded, match=reason):
        check_step_budget(
            limits_for_preset("conservative"),
            kind="model",
            model_calls=0,
            tool_calls=0,
            tokens=tokens,
            cost_usd=cost_usd,
            deadline=deadline,
        )


def test_cost_estimate_uses_cached_input_price() -> None:
    # GPT-5.5: 800 normal input at $5/M, 200 cached at $0.50/M, 100 output at $30/M.
    assert estimate_model_cost("openai:gpt-5.5", 1000, 100, 200) == pytest.approx(0.0071)


def test_anthropic_cache_reads_are_additional_to_ordinary_input() -> None:
    # Anthropic reports input_tokens and cache_read_input_tokens separately.
    assert estimate_model_cost("anthropic:claude-sonnet-5", 1000, 100, 200) == pytest.approx(
        0.00304
    )
    assert count_total_tokens("anthropic:claude-sonnet-5", 1000, 100, 200) == 1300
    assert count_total_tokens("openai:gpt-5.5", 1000, 100, 200) == 1100


def test_anthropic_cache_creation_is_counted_and_priced_conservatively() -> None:
    assert estimate_model_cost("anthropic:claude-sonnet-5", 1000, 100, 200, 300) == pytest.approx(
        0.00424
    )
    assert count_total_tokens("anthropic:claude-sonnet-5", 1000, 100, 200, 300) == 1600
    assert estimate_model_cost("anthropic:claude-sonnet-5", 1000, 100, 200, None) is None


def test_cost_estimate_rejects_unknown_rates_and_incomplete_usage() -> None:
    with pytest.raises(PricingUnavailable):
        estimate_model_cost("unknown:model", 100, 10, 0)
    assert estimate_model_cost("anthropic:claude-sonnet-5", None, 10, None) is None
    with pytest.raises(ValueError, match="cached tokens exceed input tokens"):
        estimate_model_cost("openai:gpt-5.5", 10, 5, 11)
    assert count_total_tokens("anthropic:claude-sonnet-5", 10, 5, 11) == 26
