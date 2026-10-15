import json
import re
from dataclasses import dataclass
from typing import Literal, Never
from uuid import UUID

from app.domain.models import Message
from app.domain.runtime import ModelMessage, ModelRequest, ModelStreamEvent


def _reject_json_constant(_value: str) -> Never:
    raise ValueError("native checkpoint JSON must be finite")


@dataclass(frozen=True, slots=True)
class ContextItem:
    source_id: str
    turn_id: str
    message: ModelMessage
    required: bool = False
    source_kind: Literal["message", "run_message", "checkpoint"] = "message"


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
    pricing_context_tokens: int = 0

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
            "checkpoint_replay": (
                "native" if self.request.native_context_json is not None else "portable"
            )
            if self.checkpoint_id is not None
            else None,
            "fits": self.fits,
            "needs_compaction": self.needs_compaction,
            "estimated_call_cost_usd": self.estimated_call_cost_usd,
            "pricing_context_tokens": self.pricing_context_tokens,
        }


ContextSourceKind = Literal["message", "run_message", "checkpoint"]
NativeCompactionKind = Literal["openai_window", "anthropic_block"]


@dataclass(frozen=True, slots=True)
class CheckpointSource:
    source_id: str
    source_kind: ContextSourceKind
    turn_id: str

    def __post_init__(self) -> None:
        UUID(self.source_id)
        if (
            self.source_kind not in {"message", "run_message", "checkpoint"}
            or not 1 <= len(self.turn_id) <= 200
        ):
            raise ValueError("invalid checkpoint source")


@dataclass(frozen=True, slots=True)
class ContextCheckpointDraft:
    id: str
    model_call_id: str
    sources: tuple[CheckpointSource, ...]
    summary: str
    source_hash: str
    input_hash: str
    instruction_hash: str
    workspace_hash: str
    input_tokens_before: int
    input_tokens_after: int
    native_kind: NativeCompactionKind | None = None
    native_payload_json: str | None = None
    pricing_context_tokens: int = 0

    def __post_init__(self) -> None:
        UUID(self.id)
        UUID(self.model_call_id)
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > 32768:
            raise ValueError("checkpoint summary must hold 1 to 32768 UTF-8 bytes")
        if not 1 <= len(self.sources) <= 512 or len(
            {(item.source_kind, item.source_id) for item in self.sources}
        ) != len(self.sources):
            raise ValueError("checkpoint source window is invalid")
        for digest in (
            self.source_hash,
            self.input_hash,
            self.instruction_hash,
            self.workspace_hash,
        ):
            if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                raise ValueError("checkpoint hashes must be SHA-256 digests")
        for count in (
            self.input_tokens_before,
            self.input_tokens_after,
            self.pricing_context_tokens,
        ):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("checkpoint token counts must be non-negative integers")
        if self.native_kind is None and self.native_payload_json is None:
            return
        if (
            self.native_kind not in {"openai_window", "anthropic_block"}
            or self.native_payload_json is None
        ):
            raise ValueError("native checkpoint kind and payload must be paired")
        if len(self.native_payload_json.encode("utf-8")) > 262144:
            raise ValueError("native checkpoint payload exceeds its size limit")
        try:
            value = json.loads(self.native_payload_json, parse_constant=_reject_json_constant)
        except ValueError:
            raise ValueError("native checkpoint payload is invalid JSON") from None
        if self.native_kind == "openai_window" and (not isinstance(value, list) or not value):
            raise ValueError("OpenAI checkpoint must preserve its complete returned window")
        if self.native_kind == "anthropic_block" and (
            not isinstance(value, dict) or value.get("type") != "compaction"
        ):
            raise ValueError("Anthropic checkpoint must preserve its signed compaction block")


@dataclass(frozen=True, slots=True)
class ContextCheckpoint:
    session_id: str
    run_id: str
    revision: int
    parent_id: str | None
    provider_id: str
    model_id: str
    adapter_kind: str
    covered_message_ordinal: int
    covered_run_message_ordinal: int
    created_at: str
    draft: ContextCheckpointDraft

    @property
    def id(self) -> str:
        return self.draft.id


@dataclass(frozen=True, slots=True)
class ContextWindow:
    checkpoint: ContextCheckpoint | None
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class NativeCompactionResult:
    native_kind: NativeCompactionKind
    payload_json: str | None
    usage_events: tuple[ModelStreamEvent, ...]
    provider_response_id: str | None
    failure_code: str | None = None
