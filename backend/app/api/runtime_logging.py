import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.domain.runtime import RunSnapshot

logger = logging.getLogger(__name__)
_RPC_METHODS = frozenset(
    {"auth.authenticate", "run.start", "run.lookup", "run.resume", "run.cancel", "run.respond"}
)


@dataclass(slots=True)
class CommandObservation:
    method: str
    started_at: float
    outcome_name: str = "completed"
    error_code: str | None = None
    run_id: str | None = None

    def outcome(self, name: str, error_code: str | None = None) -> None:
        self.outcome_name = name
        self.error_code = error_code

    def correlate(self, run: RunSnapshot) -> None:
        # Called only after the runtime service has established run ownership.
        self.run_id = run.id

    def identify_run(self, run_id: object) -> None:
        # Called with the ID returned by a successful owned run operation.
        if isinstance(run_id, str):
            self.run_id = run_id


@contextmanager
def runtime_command(method: str) -> Iterator[CommandObservation]:
    safe_method = method if method in _RPC_METHODS else "unknown"
    command = CommandObservation(safe_method, time.monotonic())
    try:
        yield command
    except asyncio.CancelledError:
        command.outcome("cancelled", "command_cancelled")
        raise
    except BaseException:
        command.outcome("error", "command_failed")
        raise
    finally:
        metadata: dict[str, object] = {
            "event": "runtime.command.finished",
            "command": safe_method,
            "outcome": command.outcome_name,
            "duration_ms": (time.monotonic() - command.started_at) * 1000,
        }
        if command.error_code is not None:
            metadata["error_code"] = command.error_code
        if command.run_id is not None:
            metadata["run_id"] = command.run_id
        if command.outcome_name in {"error", "failed"}:
            logger.error("runtime.command.finished", extra=metadata)
        elif command.outcome_name in {"rejected", "interrupted"}:
            logger.warning("runtime.command.finished", extra=metadata)
        else:
            logger.info("runtime.command.finished", extra=metadata)
