"""Evidence used to reconcile a local operation without repeating its effects."""

from dataclasses import dataclass
from typing import Literal

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
