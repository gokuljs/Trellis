from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["openai", "anthropic"]
MessageRole = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class UserProfile:
    id: str
    display_name: str | None
    email: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


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
class AppSettings:
    selected_provider: ProviderName
    providers: list[ProviderStatus]


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session: Session
    messages: list[Message]
