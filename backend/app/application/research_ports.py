from typing import Protocol

from app.domain.research import (
    ResearchReport,
    ResearchReservation,
    ResearchReview,
    ResearchSnapshot,
    ResearchTask,
    ResearchTaskInput,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelMessage,
    RunEvent,
    RunMessageRecord,
    RunSnapshot,
    ToolCallRecord,
)


class ResearchRepository(Protocol):
    async def research_ready(self) -> bool: ...

    async def open_research_run(
        self, run_id: str, generation: int, objective: str
    ) -> ResearchSnapshot: ...

    async def read_research(self, run_id: str) -> ResearchSnapshot | None: ...

    async def create_research_tasks(
        self, run_id: str, generation: int, operation_id: str, tasks: tuple[ResearchTaskInput, ...]
    ) -> tuple[ResearchTask, ...]: ...

    async def checkpoint_research(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        expected_revision: int,
        operation_id: str,
        checkpoint: dict[str, object],
    ) -> ResearchTask: ...

    async def reserve_research_call(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        operation_id: str,
        request_snapshot: dict[str, object],
        reserved_tokens: int,
        reserved_cost: float,
        *,
        finalization: bool = False,
    ) -> tuple[ResearchReservation, ModelCallRecord]: ...

    async def commit_research_generation(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        call_id: str,
        completion: dict[str, object],
        *,
        assistant_message: ModelMessage | None = None,
        terminal_result: dict[str, object] | None = None,
    ) -> tuple[ModelCallRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]: ...

    async def mark_research_unknown(
        self, run_id: str, generation: int, call_id: str, error_code: str = "model_outcome_unknown"
    ) -> None: ...

    async def read_research_task_messages(
        self, run_id: str, task_id: str
    ) -> list[RunMessageRecord]: ...

    async def read_research_task_calls(
        self, run_id: str, task_id: str
    ) -> list[ModelCallRecord]: ...

    async def pause_research(
        self, run_id: str, generation: int, clarification_id: str, question: str
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def answer_research_clarification(
        self, run_id: str, clarification_id: str, client_request_id: str, text: str
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...

    async def publish_research_report(
        self,
        run_id: str,
        generation: int,
        model_call_id: str,
        report: ResearchReport,
        review: ResearchReview,
        *,
        partial: bool = False,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]: ...
