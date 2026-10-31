import math
import secrets
from dataclasses import asdict
from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.domain.research import (
    CURRENT_RESEARCH_POLICY,
    RESEARCH_POLICY_V2,
    ResearchReport,
    ResearchReservation,
    ResearchReview,
    ResearchSnapshot,
    ResearchTask,
    ResearchTaskInput,
    research_identifier,
    research_limits,
    research_object,
    research_review_payload,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelMessage,
    ModelToolCall,
    RunEvent,
    RunMessageRecord,
    RunSnapshot,
    ToolCallRecord,
)

_MAX_TASK_CALLS = research_limits("longer").model_calls
_MAX_TASK_MESSAGES = _MAX_TASK_CALLS + research_limits("longer").tool_calls


def _object(value: object) -> dict[str, object]:
    try:
        return research_object(value, maximum=16 * 1024 * 1024)
    except ValueError:
        raise RuntimeError("Supabase returned invalid research data") from None


def _rows(value: object, maximum: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RuntimeError("Supabase returned an invalid research collection")
    return [_object(row) for row in value]


class ResearchRepositoryMixin:
    _user_id: UUID
    _model_call_runs: dict[str, str]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        raise NotImplementedError

    def _run_capability(self, run_id: str) -> str:
        raise NotImplementedError

    def _model_call(self, row: dict[str, object]) -> ModelCallRecord:
        raise NotImplementedError

    def _tool_call(self, row: dict[str, object]) -> ToolCallRecord:
        raise NotImplementedError

    def _event(self, row: dict[str, object]) -> RunEvent:
        raise NotImplementedError

    def _run(self, row: dict[str, object]) -> RunSnapshot:
        raise NotImplementedError

    def _run_message(
        self, row: dict[str, object], tool_calls: tuple[ModelToolCall, ...] = ()
    ) -> RunMessageRecord:
        raise NotImplementedError

    def _record_lease(self, run: RunSnapshot, capability: str) -> None:
        raise NotImplementedError

    def _drop_lease(self, run_id: str) -> None:
        raise NotImplementedError

    async def _research_call(
        self, action: str, payload: dict[str, object], *, capability: str | None = None
    ) -> object:
        run_id = research_identifier(payload.get("run_id"))
        selected = capability if capability is not None else self._run_capability(run_id)
        for attempt in range(2):
            try:
                return await self._request(
                    "POST",
                    "rpc/research_mutate",
                    payload={
                        "p_action": action,
                        "p_payload": payload,
                        "p_capability": selected,
                    },
                )
            except ApplicationError as error:
                if attempt or error.code != "database_unavailable":
                    raise
        raise AssertionError("bounded research mutation retry exhausted")

    def _research_snapshot(self, value: object, run_id: str) -> ResearchSnapshot:
        try:
            snapshot = ResearchSnapshot.from_dict(_object(value))
        except ValueError:
            raise RuntimeError("Supabase returned invalid research state") from None
        if snapshot.run_id != run_id or snapshot.user_id != str(self._user_id):
            raise RuntimeError("Supabase returned another account's research")
        return snapshot

    async def research_ready(self) -> bool:
        try:
            value = await self._request("POST", "rpc/research_readiness", payload={})
            return (
                isinstance(value, dict)
                and type(value.get("schema_version")) is int
                and value.get("schema_version")
                == (2 if CURRENT_RESEARCH_POLICY == RESEARCH_POLICY_V2 else 1)
                and (
                    CURRENT_RESEARCH_POLICY != RESEARCH_POLICY_V2
                    or value.get("policy_version") == RESEARCH_POLICY_V2
                )
                and value.get("ready") is True
            )
        except ApplicationError, RuntimeError, ValueError, TypeError:
            return False

    async def open_research_run(
        self, run_id: str, generation: int, objective: str
    ) -> ResearchSnapshot:
        value = await self._research_call(
            "open",
            {
                "run_id": run_id,
                "generation": generation,
                "objective": objective,
            },
        )
        return self._research_snapshot(value, run_id)

    async def read_research(self, run_id: str) -> ResearchSnapshot | None:
        research_identifier(run_id)
        value = await self._request("POST", "rpc/read_research", payload={"p_run_id": run_id})
        return self._research_snapshot(value, run_id) if value is not None else None

    async def create_research_tasks(
        self, run_id: str, generation: int, operation_id: str, tasks: tuple[ResearchTaskInput, ...]
    ) -> tuple[ResearchTask, ...]:
        research_identifier(operation_id)
        if not 1 <= len(tasks) <= 2 or len({task.id for task in tasks}) != len(tasks):
            raise ValueError("research task batch must contain one or two unique tasks")
        rows = _rows(
            await self._research_call(
                "create_tasks",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "operation_id": operation_id,
                    "tasks": [task.to_dict() for task in tasks],
                },
            ),
            2,
        )
        saved = tuple(ResearchTask.from_dict(row) for row in rows)
        if {task.id for task in saved} != {task.id for task in tasks} or any(
            task.run_id != run_id for task in saved
        ):
            raise RuntimeError("Supabase returned another research assignment")
        return saved

    async def checkpoint_research(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        expected_revision: int,
        operation_id: str,
        checkpoint: dict[str, object],
    ) -> ResearchTask:
        research_identifier(task_id)
        research_identifier(operation_id)
        value = await self._research_call(
            "checkpoint",
            {
                "run_id": run_id,
                "generation": generation,
                "task_id": task_id,
                "expected_revision": expected_revision,
                "operation_id": operation_id,
                "checkpoint": research_object(checkpoint),
            },
        )
        saved = ResearchTask.from_dict(_object(value))
        if saved.id != task_id or saved.run_id != run_id:
            raise RuntimeError("Supabase returned another research checkpoint")
        return saved

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
        handoff: bool = False,
    ) -> tuple[ResearchReservation, ModelCallRecord]:
        research_identifier(task_id)
        research_identifier(operation_id)
        if (
            type(reserved_tokens) is not int
            or reserved_tokens < 1
            or not math.isfinite(reserved_cost)
            or reserved_cost < 0
        ):
            raise ValueError("invalid research call reservation")
        value = _object(
            await self._research_call(
                "reserve_call",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "request_snapshot": research_object(request_snapshot, maximum=4 * 1024 * 1024),
                    "reserved_tokens": reserved_tokens,
                    "reserved_cost": reserved_cost,
                    "finalization": finalization,
                    **({"handoff": True} if handoff else {}),
                },
            )
        )
        reservation = ResearchReservation.from_dict(_object(value.get("reservation")))
        call = self._model_call(_object(value.get("model_call")))
        if (
            reservation.id != operation_id
            or reservation.run_id != run_id
            or reservation.task_id != task_id
            or call.run_id != run_id
            or reservation.model_call_id != call.id
            or reservation.reserved_tokens != reserved_tokens
            or reservation.reserved_cost != reserved_cost
            or reservation.finalization != finalization
            or call.request_snapshot != request_snapshot
        ):
            raise RuntimeError("Supabase changed the research call reservation")
        self._model_call_runs[call.id] = run_id
        return reservation, call

    async def set_research_handoff(
        self, run_id: str, generation: int, task_id: str, reserved_tokens: int, reserved_cost: float
    ) -> dict[str, object]:
        research_identifier(task_id)
        if (
            type(reserved_tokens) is not int
            or reserved_tokens < 1
            or not math.isfinite(reserved_cost)
            or reserved_cost < 0
        ):
            raise ValueError("invalid research handoff reservation")
        return _object(
            await self._research_call(
                "set_handoff",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "task_id": task_id,
                    "reserved_tokens": reserved_tokens,
                    "reserved_cost": reserved_cost,
                },
            )
        )

    async def release_research_handoff(self, run_id: str, generation: int, task_id: str) -> None:
        research_identifier(task_id)
        await self._research_call(
            "release_handoff", {"run_id": run_id, "generation": generation, "task_id": task_id}
        )

    async def research_budget_state(self, run_id: str, generation: int) -> dict[str, object]:
        return _object(
            await self._research_call("budget_state", {"run_id": run_id, "generation": generation})
        )

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
    ) -> tuple[ModelCallRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
        if (assistant_message is None) == (terminal_result is None):
            raise ValueError("research generation requires exactly one completion outcome")
        saved_completion = dict(completion)
        if "cached_tokens" in saved_completion:
            saved_completion["cached_read_tokens"] = saved_completion.pop("cached_tokens")
        value = _object(
            await self._research_call(
                "commit_generation",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "task_id": task_id,
                    "call_id": call_id,
                    "completion": saved_completion,
                    "assistant_message": asdict(assistant_message)
                    if assistant_message is not None
                    else None,
                    "terminal_result": research_object(terminal_result)
                    if terminal_result is not None
                    else None,
                },
            )
        )
        call = self._model_call(_object(value.get("model_call")))
        tools = tuple(self._tool_call(row) for row in _rows(value.get("tool_calls"), 128))
        events = tuple(self._event(row) for row in _rows(value.get("events"), 130))
        if (
            call.id != call_id
            or call.run_id != run_id
            or any(item.run_id != run_id for item in (*tools, *events))
        ):
            raise RuntimeError("Supabase returned another research completion")
        return call, tools, events

    async def mark_research_unknown(
        self, run_id: str, generation: int, call_id: str, error_code: str = "model_outcome_unknown"
    ) -> None:
        await self._research_call(
            "mark_unknown",
            {
                "run_id": run_id,
                "generation": generation,
                "call_id": call_id,
                "error_code": error_code,
            },
        )

    async def reject_research_generation(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        call_id: str,
        completion: dict[str, object],
        error_code: str,
    ) -> None:
        saved_completion = dict(completion)
        if "cached_tokens" in saved_completion:
            saved_completion["cached_read_tokens"] = saved_completion.pop("cached_tokens")
        await self._research_call(
            "reject_generation",
            {
                "run_id": run_id,
                "generation": generation,
                "task_id": task_id,
                "call_id": call_id,
                "completion": saved_completion,
                "error_code": error_code,
            },
        )

    async def _research_task(self, run_id: str, task_id: str) -> dict[str, object]:
        research_identifier(run_id)
        research_identifier(task_id)
        value = _object(
            await self._request(
                "POST",
                "rpc/read_research_task",
                payload={
                    "p_run_id": run_id,
                    "p_task_id": task_id,
                },
            )
        )
        task = ResearchTask.from_dict(_object(value.get("task")))
        if task.id != task_id or task.run_id != run_id:
            raise RuntimeError("Supabase returned another research task")
        return value

    async def read_research_task_messages(
        self, run_id: str, task_id: str
    ) -> list[RunMessageRecord]:
        value = await self._research_task(run_id, task_id)
        messages = []
        for row in _rows(value.get("messages"), _MAX_TASK_MESSAGES):
            tools = tuple(
                ModelToolCall(
                    cast(str, tool.get("id")),
                    cast(str, tool.get("name")),
                    _object(tool.get("arguments")),
                )
                for tool in _rows(row.get("tool_calls"), 128)
            )
            message = self._run_message(row, tools)
            if message.run_id != run_id or row.get("task_id") != task_id:
                raise RuntimeError("Supabase returned another task's transcript")
            messages.append(message)
        return messages

    async def read_research_task_calls(self, run_id: str, task_id: str) -> list[ModelCallRecord]:
        value = await self._research_task(run_id, task_id)
        calls = []
        for row in _rows(value.get("calls"), _MAX_TASK_CALLS):
            call = self._model_call(row)
            if call.run_id != run_id or row.get("task_id") != task_id:
                raise RuntimeError("Supabase returned another task's model calls")
            self._model_call_runs[call.id] = run_id
            calls.append(call)
        return calls

    async def pause_research(
        self, run_id: str, generation: int, clarification_id: str, question: str
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        research_identifier(clarification_id)
        value = _object(
            await self._research_call(
                "pause",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "clarification_id": clarification_id,
                    "question": question,
                },
            )
        )
        run = self._run(_object(value.get("run")))
        events = tuple(self._event(row) for row in _rows(value.get("events"), 2))
        self._drop_lease(run_id)
        return run, events

    async def answer_research_clarification(
        self, run_id: str, clarification_id: str, client_request_id: str, text: str
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        research_identifier(clarification_id)
        research_identifier(client_request_id)
        capability = secrets.token_urlsafe(32)
        value = _object(
            await self._research_call(
                "answer",
                {
                    "run_id": run_id,
                    "clarification_id": clarification_id,
                    "operation_id": client_request_id,
                    "text": text,
                },
                capability=capability,
            )
        )
        run = self._run(_object(value.get("run")))
        events = tuple(self._event(row) for row in _rows(value.get("events"), 2))
        if value.get("lease_acquired") is True:
            self._record_lease(run, capability)
        return run, events

    async def publish_research_report(
        self,
        run_id: str,
        generation: int,
        model_call_id: str,
        report: ResearchReport,
        review: ResearchReview,
        *,
        partial: bool = False,
    ) -> tuple[RunSnapshot, tuple[RunEvent, ...]]:
        if review.report_hash != report.content_hash or not {
            claim.id for claim in report.claims
        }.issubset(review.accepted_claim_ids):
            raise ValueError("research report has not passed its support review")
        value = _object(
            await self._research_call(
                "publish",
                {
                    "run_id": run_id,
                    "generation": generation,
                    "model_call_id": model_call_id,
                    "report": asdict(report),
                    "review": research_review_payload(review),
                    "report_hash": report.content_hash,
                    "partial": partial,
                },
            )
        )
        run = self._run(_object(value.get("run")))
        events = tuple(self._event(row) for row in _rows(value.get("events"), 4))
        self._drop_lease(run_id)
        return run, events
