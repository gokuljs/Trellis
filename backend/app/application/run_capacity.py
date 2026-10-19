from dataclasses import dataclass
from time import monotonic

from app.application.errors import ApplicationError

UNCONFIRMED_ACCEPTANCE_SECONDS = 60


@dataclass(slots=True)
class RunReservation:
    owner_id: str
    session_id: str
    client_request_id: str
    run_id: str | None = None
    uncertain_until: float | None = None


class RunCapacity:
    def __init__(self, *, max_active_runs: int = 16, max_active_runs_per_user: int = 4) -> None:
        if min(max_active_runs, max_active_runs_per_user) < 1:
            raise ValueError("Active run limits must be positive")
        self._maximum = max_active_runs
        self._per_user = max_active_runs_per_user
        self._reservations: dict[tuple[str, str, str], RunReservation] = {}

    def reserve(self, owner_id: str, session_id: str, client_request_id: str) -> RunReservation:
        now = monotonic()
        for reservation in tuple(self._reservations.values()):
            if reservation.uncertain_until is not None and reservation.uncertain_until <= now:
                self.release(reservation)
        key = (owner_id, session_id, client_request_id)
        existing = self._reservations.get(key)
        if existing is not None:
            existing.uncertain_until = None
            return existing
        if sum(item.owner_id == owner_id for item in self._reservations.values()) >= self._per_user:
            raise ApplicationError(
                "run_capacity", "You have reached the active run limit. Stop or finish a run first."
            )
        if len(self._reservations) >= self._maximum:
            raise ApplicationError(
                "run_capacity",
                "Trellis is working at capacity. Please try again when a run finishes.",
            )
        reservation = RunReservation(owner_id, session_id, client_request_id)
        self._reservations[key] = reservation
        return reservation

    def bind(self, reservation: RunReservation, run_id: str) -> None:
        reservation.run_id = run_id
        reservation.uncertain_until = None

    def defer_release(self, reservation: RunReservation) -> None:
        if reservation.run_id is None:
            reservation.uncertain_until = monotonic() + UNCONFIRMED_ACCEPTANCE_SECONDS

    def release(self, reservation: RunReservation) -> None:
        key = (reservation.owner_id, reservation.session_id, reservation.client_request_id)
        if self._reservations.get(key) is reservation:
            del self._reservations[key]

    def release_run(self, owner_id: str, run_id: str) -> None:
        for reservation in tuple(self._reservations.values()):
            if reservation.owner_id == owner_id and reservation.run_id == run_id:
                self.release(reservation)

    def release_owner(self, owner_id: str) -> None:
        for reservation in tuple(self._reservations.values()):
            if reservation.owner_id == owner_id:
                self.release(reservation)
