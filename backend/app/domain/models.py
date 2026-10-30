from dataclasses import dataclass, field
from typing import Literal

from app.domain.capabilities import RuntimeCapability
from app.domain.outputs import AssistantOutput

# Provider and model identifiers are owned by Trellis' catalog and are deliberately
# open-ended so adding a provider does not require changing the domain types.
ProviderName = str
ModelId = str
MessageRole = Literal["user", "assistant"]
OnboardingStep = Literal["intro", "profile", "model", "complete"]


@dataclass(frozen=True, slots=True)
class UserProfile:
    id: str
    display_name: str | None
    email: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OnboardingProgress:
    current_step: OnboardingStep
    completed: bool


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    workspace_path: str | None = None
    model_id: ModelId | None = None


class SessionWorkspaceBusy(Exception):
    """A session's workspace cannot change while a run can still use it."""


class SessionModelBusy(Exception):
    """The selected model is locked while a chat has an active run."""


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    session_id: str
    turn_id: str
    ordinal: int
    role: MessageRole
    content: str
    provider: ProviderName | None
    model: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    session: Session
    user_message: Message
    assistant_message: Message


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    id: ProviderName
    name: str
    model: str
    configured: bool
    key_hint: str | None


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: ModelId
    provider_id: ProviderName
    provider_name: str
    adapter_kind: str
    upstream_model_id: str
    name: str
    requires_api_key: bool
    supports_streaming: bool
    supports_tools: bool
    enabled: bool
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_native_compaction: bool | None = None
    metadata_status: Literal["ready", "pending"] | None = None
    pending_reasons: tuple[str, ...] = ()
    verification_sources: tuple[str, ...] = ()
    metadata_verified_at: str | None = None
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    availability_checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class ModelStatus:
    id: ModelId
    provider_id: ProviderName
    provider_name: str
    adapter_kind: str
    upstream_model_id: str
    name: str
    requires_api_key: bool
    supports_streaming: bool
    supports_tools: bool
    configured: bool
    key_hint: str | None

    enabled: bool = True
    selectable: bool = False
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_native_compaction: bool | None = None
    metadata_status: Literal["ready", "pending"] = "pending"
    pending_reasons: tuple[str, ...] = ()
    verification_sources: tuple[str, ...] = ()
    metadata_verified_at: str | None = None
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    availability_checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class AppSettings:
    selected_provider: ProviderName
    providers: list[ProviderStatus]
    selected_model_id: ModelId
    models: list[ModelStatus]
    default_budget_preset: Literal["conservative", "longer"] = "conservative"
    capabilities: tuple[RuntimeCapability, ...] = ()
    research_available: bool = False
    research_unavailable_reason: str | None = "Research is unavailable on this backend."


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session: Session
    messages: list[Message]
    assistant_outputs: list[AssistantOutput] = field(default_factory=list)
