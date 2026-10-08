from dataclasses import dataclass
from typing import Literal

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


class SessionWorkspaceBusy(Exception):
    """A session's workspace cannot change while a run can still use it."""


@dataclass(frozen=True, slots=True)
class TestPreset:
    name: str
    command: str
    cwd: str


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


@dataclass(frozen=True, slots=True)
class AppSettings:
    selected_provider: ProviderName
    providers: list[ProviderStatus]
    selected_model_id: ModelId
    models: list[ModelStatus]
    default_budget_preset: Literal["conservative", "longer"] = "conservative"


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session: Session
    messages: list[Message]
