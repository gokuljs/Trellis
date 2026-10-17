from dataclasses import replace

import pytest

from app.application.context_budget import input_fingerprint, preflight_context
from app.application.errors import ApplicationError
from app.application.model_catalog import ModelProfile, PriceRates, PriceTier, profile_for_model
from app.domain.context import ContextItem, InputTokenCount
from app.domain.runtime import ModelMessage, ModelRequest, ModelToolCall, ModelToolSpec


def profile(window: int = 10_000) -> ModelProfile:
    existing = profile_for_model("openai:gpt-5.5")
    assert existing is not None
    return replace(existing, context_window_tokens=window, max_input_tokens=None)


def item(
    source_id: str,
    turn: str,
    role: str,
    content: str,
    *,
    required: bool = False,
    tool_calls: tuple[ModelToolCall, ...] = (),
    tool_call_id: str | None = None,
) -> ContextItem:
    assert role in {"user", "assistant", "tool"}
    return ContextItem(
        source_id=source_id,
        turn_id=turn,
        message=ModelMessage(role, content, tool_call_id, tool_calls),
        required=required,
    )


def request(items: tuple[ContextItem, ...]) -> ModelRequest:
    return ModelRequest(
        "openai",
        "openai:gpt-5.5",
        "openai",
        "gpt-5.5",
        tuple(source.message for source in items),
        4096,
        system_instructions="Follow the user's request.",
    )


def test_preflight_reserves_output_and_safety_space_and_records_used_sources() -> None:
    items = (item("m1", "t1", "user", "Hello", required=True),)
    plan = preflight_context(
        request(items), items=items, profile=profile(), remaining_run_tokens=100_000
    )
    assert plan.output_reserve_tokens == 4096
    assert plan.safety_margin_tokens == 2048
    assert plan.usable_input_tokens == 3856
    assert plan.fits
    assert not plan.needs_compaction
    snapshot = plan.snapshot()
    assert snapshot["sources"] == [{"source_id": "m1", "source_kind": "message", "turn_id": "t1"}]
    assert snapshot["count_method"] == "utf8_bytes_estimate_v1"
    assert snapshot["checkpoint_id"] is None
    assert snapshot["checkpoint_replay"] is None
    assert len(str(snapshot["input_hash"])) == 64
    tokens = snapshot["input_tokens"]
    assert isinstance(tokens, int) and tokens > 5


def test_provider_count_is_used_only_for_the_matching_input() -> None:
    items = (item("m1", "t1", "user", "Hello", required=True),)
    model_request = request(items)
    count = InputTokenCount(10, "provider", input_fingerprint(model_request))
    plan = preflight_context(
        model_request,
        items=items,
        profile=profile(),
        remaining_run_tokens=100_000,
        counted_input=count,
    )
    assert plan.input_tokens == 10
    assert plan.count_method == "provider"
    with pytest.raises(ApplicationError, match="different context"):
        preflight_context(
            model_request,
            items=items,
            profile=profile(),
            remaining_run_tokens=100_000,
            counted_input=replace(count, input_hash="0" * 64),
        )


def test_input_hash_changes_with_tools_and_source_content_but_not_output_reservation() -> None:
    items = (item("m1", "t1", "user", "Hello", required=True),)
    original = request(items)
    assert input_fingerprint(original) == input_fingerprint(replace(original, max_output_tokens=32))
    assert input_fingerprint(original) != input_fingerprint(
        replace(original, messages=(ModelMessage("user", "Different"),))
    )
    assert input_fingerprint(original) != input_fingerprint(
        replace(original, tools=(ModelToolSpec("read", "Read", {"type": "object"}),))
    )


def test_old_prefix_is_compactable_while_four_recent_turns_and_current_request_are_protected() -> (
    None
):
    items = (
        *(item(f"m{index}", f"t{index}", "user", "short") for index in range(1, 7)),
        item("current", "current-turn", "user", "Continue", required=True),
    )
    model_request = request(items)
    plan = preflight_context(
        model_request,
        items=items,
        profile=profile(),
        remaining_run_tokens=100_000,
        counted_input=InputTokenCount(3084, "provider", input_fingerprint(model_request)),
    )
    assert plan.needs_compaction
    assert [source.source_id for source in plan.compactable_prefix] == ["m1", "m2"]
    assert [source.source_id for source in plan.retained_tail] == [
        "m3",
        "m4",
        "m5",
        "m6",
        "current",
    ]
    assert plan.request.messages == model_request.messages


def test_small_remaining_run_budget_reduces_output_reservation() -> None:
    items = (item("m1", "t1", "user", "hello", required=True),)
    model_request = request(items)
    count = InputTokenCount(100, "provider", input_fingerprint(model_request))
    plan = preflight_context(
        model_request, items=items, profile=profile(), remaining_run_tokens=300, counted_input=count
    )
    assert plan.output_reserve_tokens == 200
    assert plan.request.max_output_tokens == 200
    assert plan.usable_input_tokens == 100
    assert plan.fits


def test_insufficient_run_budget_for_the_required_input_fails_clearly() -> None:
    items = (item("current", "t1", "user", "x" * 1000, required=True),)
    with pytest.raises(ApplicationError) as error:
        preflight_context(request(items), items=items, profile=profile(), remaining_run_tokens=100)
    assert error.value.code == "run_budget_exhausted"


def test_oversized_current_request_cannot_be_shortened() -> None:
    items = (item("current", "t1", "user", "x" * 6000, required=True),)
    with pytest.raises(ApplicationError) as error:
        preflight_context(
            request(items), items=items, profile=profile(), remaining_run_tokens=100_000
        )
    assert error.value.code == "context_input_too_large"


def test_tool_requests_and_all_results_remain_one_complete_compaction_unit() -> None:
    calls = (ModelToolCall("c1", "read", {}), ModelToolCall("c2", "read", {}))
    items = (
        item("old-user", "old", "user", "Read both"),
        item("calls", "old", "assistant", "Checking", tool_calls=calls),
        item("result2", "old", "tool", "Second", tool_call_id="c2"),
        item("result1", "old", "tool", "First", tool_call_id="c1"),
        *(item(f"m{i}", f"t{i}", "user", "recent") for i in range(4)),
        item("current", "now", "user", "Continue", required=True),
    )
    plan = preflight_context(
        request(items), items=items, profile=profile(), remaining_run_tokens=100_000
    )
    assert [source.source_id for source in plan.compactable_prefix] == [
        "old-user",
        "calls",
        "result2",
        "result1",
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        (ModelMessage("tool", "orphan", tool_call_id="missing"),),
        (ModelMessage("assistant", "", tool_calls=(ModelToolCall("c1", "read", {}),)),),
        (
            ModelMessage("assistant", "", tool_calls=(ModelToolCall("c1", "read", {}),)),
            ModelMessage("tool", "wrong", tool_call_id="c2"),
        ),
    ],
)
def test_invalid_tool_sequence_is_rejected_before_a_provider_call(
    malformed: tuple[ModelMessage, ...],
) -> None:
    items = tuple(
        ContextItem(str(i), "t1", message, required=True) for i, message in enumerate(malformed)
    )
    with pytest.raises(ApplicationError) as error:
        preflight_context(
            request(items), items=items, profile=profile(), remaining_run_tokens=100_000
        )
    assert error.value.code == "context_invalid_tool_sequence"


def test_large_tool_result_is_bounded_in_context_without_mutating_the_transcript() -> None:
    call = ModelToolCall("c1", "read", {})
    items = (
        item("current", "t1", "user", "Read", required=True),
        item("call", "t1", "assistant", "", tool_calls=(call,)),
        item("result", "t1", "tool", "😀" * 20_000, tool_call_id="c1"),
    )
    plan = preflight_context(
        request(items), items=items, profile=profile(1_000_000), remaining_run_tokens=100_000
    )
    assert len(plan.request.messages[-1].content.encode("utf-8")) <= 32768
    assert plan.request.messages[-1].content.endswith("[Tool result truncated for context.]")
    assert items[-1].message.content == "😀" * 20_000


def test_tail_token_cap_can_move_a_large_completed_turn_to_the_prefix() -> None:
    items = (
        item("large-old", "old", "user", "x" * 20_000),
        item("current", "now", "user", "Continue", required=True),
    )
    plan = preflight_context(
        request(items), items=items, profile=profile(1_000_000), remaining_run_tokens=100_000
    )
    assert [source.source_id for source in plan.compactable_prefix] == ["large-old"]
    assert [source.source_id for source in plan.retained_tail] == ["current"]


def test_preflight_cost_floor_rejects_unaffordable_required_input() -> None:
    items = (item("current", "now", "user", "Continue", required=True),)
    with pytest.raises(ApplicationError) as error:
        preflight_context(
            request(items),
            items=items,
            profile=profile(),
            remaining_run_tokens=100_000,
            remaining_run_cost_usd=0.0,
        )
    assert error.value.code == "budget_cost_limit"


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_invalid_provider_token_counts_are_rejected(tokens: int) -> None:
    with pytest.raises(ValueError, match="integer"):
        InputTokenCount(tokens, "provider", "0" * 64)


def test_profile_from_another_provider_cannot_supply_context_limits() -> None:
    items = (item("current", "t1", "user", "Hello", required=True),)
    with pytest.raises(ApplicationError) as error:
        preflight_context(
            replace(request(items), provider_id="anthropic"),
            items=items,
            profile=profile(),
            remaining_run_tokens=100_000,
        )
    assert error.value.code == "model_not_available"


def test_provider_count_of_required_context_is_authoritative_for_run_affordability() -> None:
    items = (item("current", "t1", "user", "Hello", required=True),)
    model_request = request(items)
    with pytest.raises(ApplicationError) as error:
        preflight_context(
            model_request,
            items=items,
            profile=profile(),
            remaining_run_tokens=500,
            counted_input=InputTokenCount(700, "provider", input_fingerprint(model_request)),
        )
    assert error.value.code == "run_budget_exhausted"


@pytest.mark.parametrize("write_rate,one_hour_rate", [(1.25, None), (1.25, 2.0)])
def test_output_reservation_covers_the_highest_input_cache_write_rate(
    write_rate: float, one_hour_rate: float | None
) -> None:
    items = (item("current", "now", "user", "Continue", required=True),)
    model_request = request(items)
    worst_input_rate = max(write_rate, one_hour_rate or 0)
    catalog = replace(
        profile(100_000),
        price_tiers=(
            PriceTier("standard", None, PriceRates(1, 0.1, 1, write_rate, one_hour_rate)),
        ),
    )
    plan = preflight_context(
        model_request,
        items=items,
        profile=catalog,
        remaining_run_tokens=100_000,
        counted_input=InputTokenCount(1000, "provider", input_fingerprint(model_request)),
        remaining_run_cost_usd=(1000 * worst_input_rate + 250) / 1_000_000,
    )
    assert plan.output_reserve_tokens == 250
    assert plan.estimated_call_cost_usd == pytest.approx(
        (1000 * worst_input_rate + 250) / 1_000_000
    )


def test_checkpoint_marker_stays_with_recent_turns_without_becoming_a_compactable_prefix() -> None:
    checkpoint = replace(
        item("cp", "checkpoint", "user", "Prior summary"), source_kind="checkpoint"
    )
    items = (
        checkpoint,
        *(item(f"m{i}", f"t{i}", "user", "Recent") for i in range(4)),
        item("current", "now", "user", "Continue", required=True),
    )
    plan = preflight_context(
        request(items), items=items, profile=profile(), remaining_run_tokens=100_000
    )
    assert plan.compactable_prefix == ()
    assert plan.retained_tail == items


def test_session_pricing_high_water_mark_survives_a_small_compacted_request() -> None:
    items = (item("current", "now", "user", "Continue", required=True),)
    ordinary = preflight_context(
        request(items), items=items, profile=profile(), remaining_run_tokens=100_000
    )
    compacted = preflight_context(
        request(items),
        items=items,
        profile=profile(),
        remaining_run_tokens=100_000,
        pricing_context_tokens=300_000,
    )
    assert (
        compacted.estimated_call_cost_usd is not None
        and ordinary.estimated_call_cost_usd is not None
    )
    assert compacted.estimated_call_cost_usd > ordinary.estimated_call_cost_usd
    assert compacted.snapshot()["pricing_context_tokens"] == 300_000
