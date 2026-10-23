"""Evidence used to reconcile a local operation without repeating its effects."""

from dataclasses import dataclass
from typing import Literal

from app.domain.runtime import ToolCallRecord

PatchState = Literal["desired_state_present", "unchanged_original", "conflict"]


@dataclass(frozen=True, slots=True)
class PatchReconciliation:
    state: PatchState
    before_sha256: str | None
    after_sha256: str | None
    observed_sha256: str | None

    def proof(self) -> dict[str, object]:
        return {
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "observed_sha256": self.observed_sha256,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceReviewBlock:
    session_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReviewState:
    blocked: bool
    reason: str | None = None
    source_session_id: str | None = None
    run_id: str | None = None
    tool: ToolCallRecord | None = None
    workspace_path: str | None = None
