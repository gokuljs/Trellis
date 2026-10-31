"""Trusted identity and lifecycle of one durably dispatched tool operation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.application.budgets import RunLimits


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    user_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    dispatch_id: str
    dispatch_attempt: int
    generation: int
    deadline_at: datetime
    budget_limits: RunLimits
    workspace_root: Path | None
    _assert_run_active: Callable[[], Awaitable[None]] = field(repr=False, compare=False)
    run_mode: Literal["chat", "research"] = "chat"
    research_policy_version: str | None = None

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, (self.deadline_at - datetime.now(UTC)).total_seconds())

    async def assert_active(self) -> None:
        if self.remaining_seconds <= 0:
            raise TimeoutError
        await self._assert_run_active()
        if self.remaining_seconds <= 0:
            raise TimeoutError
