from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.runtime import RunSnapshot

type Attribute = str | int | float | bool
type Attributes = Mapping[str, Attribute]


def run_identifiers(run: RunSnapshot) -> dict[str, str]:
    return {"chatId": run.session_id, "turnId": run.turn_id, "runId": run.id}


@dataclass(frozen=True, slots=True)
class TraceLink:
    trace_id: int
    span_id: int
    sampled: bool = True
    remote: bool = False


class Operation(Protocol):
    def set_attributes(self, attributes: Attributes) -> None: ...
    def outcome(self, name: str, error_code: str | None = None) -> None: ...
    def link(self) -> TraceLink | None: ...


class Telemetry(Protocol):
    def correlate(self, attributes: Attributes) -> AbstractContextManager[None]: ...

    def operation(
        self,
        name: str,
        attributes: Attributes | None = None,
        *,
        root: bool = False,
        links: Sequence[TraceLink] = (),
        parent: TraceLink | None = None,
        kind: Literal["internal", "server", "client"] = "internal",
    ) -> AbstractContextManager[Operation]: ...

    def capture(self) -> TraceLink | None: ...
    def event(
        self, name: str, attributes: Attributes | None = None, *, error: bool = False
    ) -> None: ...
    def count(self, name: str, value: int = 1, attributes: Attributes | None = None) -> None: ...
    def observe(self, name: str, value: float, attributes: Attributes | None = None) -> None: ...


class NoTelemetry:
    @contextmanager
    def correlate(self, attributes: Attributes) -> Iterator[None]:
        yield

    @contextmanager
    def operation(
        self,
        name: str,
        attributes: Attributes | None = None,
        *,
        root: bool = False,
        links: Sequence[TraceLink] = (),
        parent: TraceLink | None = None,
        kind: Literal["internal", "server", "client"] = "internal",
    ) -> Iterator[Operation]:
        yield self

    def set_attributes(self, attributes: Attributes) -> None:
        pass

    def outcome(self, name: str, error_code: str | None = None) -> None:
        pass

    def link(self) -> TraceLink | None:
        return None

    def capture(self) -> TraceLink | None:
        return None

    def event(
        self, name: str, attributes: Attributes | None = None, *, error: bool = False
    ) -> None:
        pass

    def count(self, name: str, value: int = 1, attributes: Attributes | None = None) -> None:
        pass

    def observe(self, name: str, value: float, attributes: Attributes | None = None) -> None:
        pass


NO_TELEMETRY = NoTelemetry()
