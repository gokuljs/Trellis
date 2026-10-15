from dataclasses import dataclass
from typing import Literal

from app.domain.runtime import ModelMessage, ModelRequest


@dataclass(frozen=True, slots=True)
class ContextItem:
    source_id: str
    turn_id: str
    message: ModelMessage
    required: bool = False
    source_kind: Literal["message", "run_message"] = "message"


@dataclass(frozen=True, slots=True)
class InputTokenCount:
    tokens: int
    method: Literal["provider", "utf8_bytes_estimate_v1"]
    input_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.tokens, int) or isinstance(self.tokens, bool) or self.tokens < 0:
            raise ValueError("input token count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ContextPreflight:
    request: ModelRequest
    sources: tuple[ContextItem, ...]
    compactable_prefix: tuple[ContextItem, ...]
    retained_tail: tuple[ContextItem, ...]
    input_tokens: int
    count_method: str
    input_hash: str
    output_reserve_tokens: int
    safety_margin_tokens: int
    usable_input_tokens: int
    fits: bool
    needs_compaction: bool
    checkpoint_id: str | None = None
    estimated_call_cost_usd: float | None = None
    source_hash: str = ""

    def snapshot(self) -> dict[str, object]:
        return {
            "policy_version": "bounded-context-v1",
            "sources": [
                {
                    "source_id": item.source_id,
                    "source_kind": item.source_kind,
                    "turn_id": item.turn_id,
                }
                for item in self.sources
            ],
            "source_hash": self.source_hash,
            "input_hash": self.input_hash,
            "input_tokens": self.input_tokens,
            "count_method": self.count_method,
            "output_reserve_tokens": self.output_reserve_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "usable_input_tokens": self.usable_input_tokens,
            "checkpoint_id": self.checkpoint_id,
            "fits": self.fits,
            "needs_compaction": self.needs_compaction,
            "estimated_call_cost_usd": self.estimated_call_cost_usd,
        }
