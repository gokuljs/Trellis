"""Stable cursors for bounded conversation reads, independent of HTTP and SQL."""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.models import Message, Session
from app.domain.outputs import AssistantOutput

TimelineKind = Literal["message", "assistant_output"]


@dataclass(frozen=True, slots=True)
class PageCursor:
    created_at: str
    kind: str
    id: str

    def encode(self) -> str:
        return (
            base64.urlsafe_b64encode(
                json.dumps([1, self.created_at, self.kind, self.id], separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

    @classmethod
    def decode(cls, value: str, *, session_list: bool = False) -> PageCursor:
        try:
            if not 1 <= len(value) <= 512:
                raise ValueError
            decoded = json.loads(
                base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            )
            if not isinstance(decoded, list) or len(decoded) != 4 or decoded[0] != 1:
                raise ValueError
            _, stamp, kind, identity = decoded
            if not isinstance(stamp, str) or not isinstance(identity, str):
                raise ValueError
            if kind not in ({"session"} if session_list else {"message", "assistant_output"}):
                raise ValueError
            date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError
            UUID(identity)
            return cls(stamp, kind, identity)
        except ValueError, TypeError, binascii.Error, UnicodeError:
            raise ValueError("invalid history cursor") from None


@dataclass(frozen=True, slots=True)
class TimelineRow:
    kind: TimelineKind
    id: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    messages: tuple[Message, ...]
    assistant_outputs: tuple[AssistantOutput, ...]
    timeline_order: tuple[TimelineRow, ...]
    next_before_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[Session, ...]
    next_before_cursor: str | None
