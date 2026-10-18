"""A restricted database login and claim-bound execution transport.

The private transport reuses the browser repository's record codecs and checked
mutation payloads. It replaces every storage boundary; no browser token, account
repository, or direct table operation is available to the public scoped facade.
"""

import asyncio
from dataclasses import asdict
from typing import LiteralString, Self
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.domain.context import (
    ContextCheckpoint,
    ContextCheckpointDraft,
    ContextWindow,
)
from app.domain.history import HistoryPage, PageCursor, SessionPage
from app.domain.jobs import ExecutionClaim, RunExecutionContext
from app.domain.models import (
    Message,
    ModelDescriptor,
    ModelId,
    Session,
)
from app.domain.outputs import AssistantOutput
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    ToolCallStatus,
)
from app.infrastructure.supabase_repository import SupabaseRepository, _object, _text

_READ_TABLES = frozenset(
    {
        "runs",
        "chats",
        "messages",
        "models",
        "model_calls",
        "run_events",
        "run_messages",
        "tool_calls",
        "run_assistant_outputs",
        "context_checkpoints",
    }
)
_WRITE_ACTIONS = frozenset(
    {
        "append_run_event",
        "transition_run_record",
        "complete_run",
        "create_model_call",
        "update_model_call",
        "record_assistant_message",
        "append_assistant_batch",
        "finish_assistant_output",
        "save_context_checkpoint",
    }
)


class PostgresWorkerQueue:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, dsn: str, *, max_size: int = 6) -> Self:
        if isinstance(max_size, bool) or not 1 <= max_size <= 16:
            raise ValueError("worker connection capacity must be between 1 and 16")
        pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            open=False,
            timeout=5,
            kwargs={
                "autocommit": True,
                "connect_timeout": 5,
                "options": "-c statement_timeout=4000 -c lock_timeout=2000",
            },
        )
        instance = cls(pool)
        try:
            await pool.open(wait=True, timeout=5)
            async with asyncio.timeout(5), pool.connection() as connection:
                row = await (
                    await connection.execute("SELECT session_user,current_user")
                ).fetchone()
                if row != ("trellis_runtime_worker", "trellis_runtime_worker"):
                    raise ValueError("worker database URL must use the restricted worker login")
        except BaseException:
            await pool.close(timeout=5)
            raise
        return instance

    async def close(self) -> None:
        await self._pool.close(timeout=5)

    async def _call(self, statement: LiteralString, parameters: tuple[object, ...]) -> object:
        for attempt in range(2):
            try:
                async with asyncio.timeout(5), self._pool.connection(timeout=5) as connection:
                    row = await (await connection.execute(statement, parameters)).fetchone()
                    if row is None:
                        raise RuntimeError("worker function returned no result")
                    return row[0]
            except psycopg.errors.InsufficientPrivilege:
                raise ApplicationError(
                    "run_not_owned", "Execution ownership expired or was revoked."
                ) from None
            except psycopg.errors.InvalidParameterValue as error:
                code = (
                    "context_checkpoint_conflict"
                    if "checkpoint revision conflict" in str(error)
                    else "event_storage_limit"
                    if "worker_operation_storage_limit" in str(error)
                    else "worker_operation_invalid"
                )
                raise ApplicationError(
                    code, "The execution operation could not be saved."
                ) from None
            except psycopg.OperationalError, psycopg.InterfaceError, TimeoutError:
                if attempt == 0:
                    await asyncio.sleep(0.05)
                    continue
                raise ApplicationError(
                    "database_unavailable", "The worker database is temporarily unavailable."
                ) from None
            except psycopg.Error:
                raise ApplicationError(
                    "worker_operation_invalid", "The execution operation could not be saved."
                ) from None

    async def claim(
        self, worker_id: str, attempt_id: str, capability: str
    ) -> ExecutionClaim | None:
        value = await self._call(
            "SELECT trellis_private.worker_claim(%s,%s,%s)", (worker_id, attempt_id, capability)
        )
        if value is None:
            return None
        row = _object(value)
        return ExecutionClaim(
            *(
                _text(row, key)
                for key in (
                    "job_id",
                    "run_id",
                    "worker_id",
                    "attempt_id",
                    "capability",
                    "lease_expires_at",
                )
            )
        )

    def bind(self, claim: ExecutionClaim) -> PostgresWorkerExecutionRepository:
        return PostgresWorkerExecutionRepository(self, claim)

    async def _snapshot(self, statement: LiteralString, claim: ExecutionClaim) -> RunSnapshot:
        value = _object(await self._call(statement, (Jsonb(asdict(claim)),)))
        transport = _ScopedTransport(self, claim)
        transport._user_id = UUID(_text(value, "user_id"))
        return transport._run(value)

    async def begin(self, claim: ExecutionClaim) -> RunSnapshot:
        return await self._snapshot("SELECT trellis_private.worker_begin(%s)", claim)

    async def renew(self, claim: ExecutionClaim) -> RunSnapshot:
        return await self._snapshot("SELECT trellis_private.worker_renew(%s)", claim)

    async def release_unstarted(self, claim: ExecutionClaim) -> None:
        await self._call(
            "SELECT trellis_private.worker_release_unstarted(%s)", (Jsonb(asdict(claim)),)
        )

    async def recover_expired(self, limit: int = 100) -> int:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("expiry recovery limit must be between 1 and 100")
        value = await self._call("SELECT trellis_private.worker_recover_expired(%s)", (limit,))
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("worker recovery returned an invalid count")
        return value


class _ScopedTransport(SupabaseRepository):
    def __init__(self, queue: PostgresWorkerQueue, claim: ExecutionClaim) -> None:
        self.queue = queue
        self.claim = claim
        self._user_id = UUID(int=0)
        self._model_call_runs = {}
        self._capabilities = {}
        self._lease_expires = {}

    def _run_capability(self, run_id: str) -> str:
        if run_id != self.claim.run_id:
            raise ApplicationError("run_not_owned", "This run is outside the execution assignment.")
        return self.claim.capability

    async def read(self, action: str, payload: dict[str, object]) -> object:
        value = _object(
            await self.queue._call(
                "SELECT trellis_private.worker_read(%s,%s,%s)",
                (Jsonb(asdict(self.claim)), action, Jsonb(payload)),
            )
        )
        self._user_id = UUID(_text(value, "owner_id"))
        return value.get("result")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        if method == "POST" and path == "rpc/read_context_window":
            request = payload or {}
            return await self.read(
                "context_window",
                {
                    "session_id": request.get("p_session_id"),
                    "limit": request.get("p_limit", 128),
                },
            )
        if method == "POST" and path == "rpc/list_chat_summaries":
            request = payload or {}
            return await self.read(
                "chats", {"session_id": request["p_chat_id"]} if "p_chat_id" in request else {}
            )
        if method != "GET" or path not in _READ_TABLES:
            raise PermissionError("worker storage access is restricted to its assigned execution")
        filters = params or {}
        request = {}
        for key, value in filters.items():
            if key in {"select", "order", "user_id", "enabled", "metadata_status"}:
                continue
            if key in {"limit", "offset"}:
                request[key] = int(value)
            elif key in {"id", "chat_id", "run_id", "turn_id"} and value.startswith("eq."):
                target = (
                    "session_id"
                    if key == "chat_id" or (key == "id" and path == "chats")
                    else "run_id"
                    if key == "id" and path == "runs"
                    else key
                )
                request[target] = value[3:]
            elif key in {"ordinal", "sequence"} and value.startswith("gt."):
                request["after_ordinal" if key == "ordinal" else "after_sequence"] = int(value[3:])
            else:
                raise PermissionError("worker query is outside the assigned execution")
        return await self.read(path, request)

    async def _rpc(
        self,
        action: str,
        payload: dict[str, object],
        *,
        capability: str | None = None,
    ) -> object:
        if action not in _WRITE_ACTIONS:
            raise PermissionError("workers cannot perform user or account operations")
        return await self.queue._call(
            "SELECT trellis_private.worker_mutate(%s,%s,%s,%s)",
            (Jsonb(asdict(self.claim)), str(uuid4()), action, Jsonb(payload)),
        )


class PostgresWorkerExecutionRepository:
    def __init__(self, queue: PostgresWorkerQueue, claim: ExecutionClaim) -> None:
        self._transport = _ScopedTransport(queue, claim)

    @property
    def claim(self) -> ExecutionClaim:
        return self._transport.claim

    async def get_execution_context(self) -> RunExecutionContext:
        row = _object(await self._transport.read("context", {}))
        models = await self._transport._models(ready_only=True)
        if len(models) != 1 or models[0].id != _object(row.get("run")).get("model_id"):
            raise RuntimeError("assigned model is unavailable")
        return RunExecutionContext(
            _text(row, "owner_id"),
            self._transport._run(_object(row.get("run"))),
            self._transport._session(_object(row.get("session"))),
            models[0],
        )

    async def assert_provider_access(self, provider: str) -> str:
        owner = await self._transport.read("credential", {"provider": provider})
        if not isinstance(owner, str):
            raise RuntimeError("worker credential authorization returned an invalid owner")
        return owner

    async def read_session_page(self, before: PageCursor | None, *, limit: int = 40) -> SessionPage:
        raise PermissionError("This operation requires user authority or local tools.")

    async def read_history_page(
        self, session_id: str, before: PageCursor | None, *, limit: int = 40
    ) -> HistoryPage:
        raise PermissionError("This operation requires user authority or local tools.")

    async def get_message(self, session_id: str, message_id: str) -> Message | None:
        return await self._transport.get_message(session_id, message_id)

    async def set_session_model(self, session_id: str, model_id: ModelId) -> Session | None:
        raise PermissionError("This operation requires user authority or local tools.")

    async def create_session(self, workspace_path: str | None = None) -> Session:
        raise PermissionError("This operation requires user authority or local tools.")

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> Session | None:
        raise PermissionError("This operation requires user authority or local tools.")

    async def list_sessions(self) -> list[Session]:
        return await self._transport.list_sessions()

    async def get_session(self, session_id: str) -> Session | None:
        return await self._transport.get_session(session_id)

    async def list_messages(self, session_id: str) -> list[Message]:
        return await self._transport.list_messages(session_id)

    async def list_assistant_outputs(self, session_id: str) -> list[AssistantOutput]:
        return await self._transport.list_assistant_outputs(session_id)

    async def create_run(
        self,
        session_id: str,
        turn_id: str,
        client_request_id: str,
        content: str,
        model: ModelDescriptor,
        *,
        budget_preset: BudgetPreset = "conservative",
    ) -> RunSnapshot:
        raise PermissionError("This operation requires user authority or local tools.")

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        return await self._transport.get_run(run_id)

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None:
        return await self._transport.get_latest_run_for_session(session_id)

    async def list_runs_for_session(
        self,
        session_id: str,
        offset: int,
        limit: int,
        *,
        turn_id: str | None = None,
        newest_first: bool = False,
    ) -> list[RunSnapshot]:
        return await self._transport.list_runs_for_session(
            session_id, offset, limit, turn_id=turn_id, newest_first=newest_first
        )

    async def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        event_version: int = 1,
    ) -> RunEvent:
        return await self._transport.append_run_event(
            run_id, event_type, data, event_version=event_version
        )

    async def list_run_events(
        self, run_id: str, after_sequence: int = 0, *, limit: int = 500
    ) -> list[RunEvent]:
        return await self._transport.list_run_events(run_id, after_sequence, limit=limit)

    async def transition_run_record(
        self,
        run_id: str,
        next_status: RunStatus,
        event_type: RunEventType,
        data: dict[str, object],
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        stop_reason: str | None = None,
    ) -> RunEvent:
        return await self._transport.transition_run_record(
            run_id,
            next_status,
            event_type,
            data,
            error_code=error_code,
            error_message=error_message,
            stop_reason=stop_reason,
        )

    async def complete_run(self, run_id: str, content: str) -> tuple[Message, tuple[RunEvent, ...]]:
        return await self._transport.complete_run(run_id, content)

    async def append_assistant_batch(
        self, run_id: str, model_call_id: str, batch_index: int, text: str
    ) -> tuple[AssistantOutput, RunEvent]:
        return await self._transport.append_assistant_batch(
            run_id, model_call_id, batch_index, text
        )

    async def finish_assistant_output(
        self, run_id: str, model_call_id: str, *, stop_reason: str = "tool_continuation"
    ) -> AssistantOutput | None:
        return await self._transport.finish_assistant_output(
            run_id, model_call_id, stop_reason=stop_reason
        )

    async def request_run_cancellation(self, run_id: str) -> tuple[RunSnapshot, RunEvent | None]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def create_model_call(
        self,
        run_id: str,
        step_index: int,
        model: ModelDescriptor,
        request_snapshot: dict[str, object],
    ) -> ModelCallRecord:
        return await self._transport.create_model_call(run_id, step_index, model, request_snapshot)

    async def update_model_call(
        self,
        call_id: str,
        next_status: ModelCallStatus,
        *,
        response_snapshot: dict[str, object] | None = None,
        provider_response_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        estimated_cost: float | None = None,
        clear_estimated_cost: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelCallRecord:
        return await self._transport.update_model_call(
            call_id,
            next_status,
            response_snapshot=response_snapshot,
            provider_response_id=provider_response_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_cost=estimated_cost,
            clear_estimated_cost=clear_estimated_cost,
            error_code=error_code,
            error_message=error_message,
        )

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        return await self._transport.list_model_calls(run_id)

    async def record_assistant_message(
        self, run_id: str, model_call_id: str, message: ModelMessage
    ) -> tuple[RunMessageRecord, tuple[ToolCallRecord, ...], tuple[RunEvent, ...]]:
        return await self._transport.record_assistant_message(run_id, model_call_id, message)

    async def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        content: str,
        *,
        status: ToolCallStatus = ToolCallStatus.COMPLETED,
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def record_tool_approval_decision(
        self, run_id: str, tool_call_id: str, decision: ToolApprovalDecision
    ) -> tuple[ToolCallRecord, RunEvent | None]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def request_tool_approval(
        self, run_id: str, tool_call_id: str, preview: dict[str, object] | None
    ) -> tuple[RunSnapshot, RunEvent]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def resume_approved_run(self, run_id: str) -> tuple[RunSnapshot, RunEvent]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def list_decided_approval_runs(self) -> list[str]:
        raise PermissionError("This operation requires user authority or local tools.")

    async def list_run_messages(self, run_id: str) -> list[RunMessageRecord]:
        return await self._transport.list_run_messages(run_id)

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        return await self._transport.list_tool_calls(run_id)

    async def load_context_window(self, session_id: str, *, limit: int = 128) -> ContextWindow:
        return await self._transport.load_context_window(session_id, limit=limit)

    async def load_context_checkpoint(self, session_id: str) -> ContextCheckpoint | None:
        return await self._transport.load_context_checkpoint(session_id)

    async def save_context_checkpoint(
        self, run_id: str, expected_revision: int, draft: ContextCheckpointDraft
    ) -> ContextCheckpoint:
        return await self._transport.save_context_checkpoint(run_id, expected_revision, draft)

    async def list_context_messages(
        self, session_id: str, after_ordinal: int, *, limit: int = 128
    ) -> list[Message]:
        return await self._transport.list_context_messages(session_id, after_ordinal, limit=limit)

    async def get_turn_messages(self, session_id: str, turn_id: str) -> list[Message]:
        return await self._transport.get_turn_messages(session_id, turn_id)
