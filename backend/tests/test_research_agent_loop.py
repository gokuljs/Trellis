import asyncio
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from pydantic import BaseModel

from app.application.errors import ApplicationError, ProviderError
from app.application.ports import (
    ContextCheckpointRepository,
    ProfileRepository,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
)
from app.application.research import ResearchCoordinator, ResearchGeneration, validate_task_recovery
from app.application.research_ports import ResearchRepository
from app.application.run_context import RunContext
from app.application.runs import RunService, _ExecutionContext
from app.application.secret_redaction import COMMON_SECRET_REDACTOR
from app.application.tool_context import ToolExecutionContext
from app.application.tools import ToolRegistration, ToolRegistry
from app.domain.models import ModelDescriptor
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.research import ResearchPhase, ResearchSnapshot, ResearchTask, ResearchTaskStatus
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunMessageRecord,
    RunSnapshot,
    RunStatus,
    ToolCallRecord,
    ToolCallStatus,
)


def make_run() -> RunSnapshot:
    now = datetime.now(UTC)
    return RunSnapshot(
        id=str(uuid4()),
        session_id=str(uuid4()),
        turn_id="question",
        status=RunStatus.RUNNING,
        provider_id="openai",
        model_id="openai:gpt-5.5",
        adapter_kind="openai_responses",
        upstream_model_id="gpt-5.5",
        input_message_id=str(uuid4()),
        retry_of=None,
        client_request_id="request",
        max_model_calls=24,
        max_tool_calls=64,
        budget_preset="conservative",
        max_total_tokens=100_000,
        max_cost_usd=2,
        deadline_at=(now + timedelta(seconds=600)).isoformat(),
        cancel_requested_at=None,
        lease_expires_at=None,
        recovery_count=0,
        stop_reason=None,
        last_event_sequence=0,
        error_code=None,
        error_message=None,
        created_at=now.isoformat(),
        started_at=now.isoformat(),
        finished_at=None,
        generation=1,
        run_mode="research",
    )


class MemoryResearch:
    def __init__(self, run: RunSnapshot) -> None:
        self.run = run
        self.task = ResearchTask(
            str(uuid4()),
            run.id,
            "Compare source evidence",
            "worker",
            1,
            ResearchTaskStatus.QUEUED,
            0,
            {},
        )
        self.calls: list[ModelCallRecord] = []
        self.messages: list[RunMessageRecord] = []
        self.tools: list[ToolCallRecord] = []
        self.events: list[RunEvent] = []
        self.fail_after_tenth_source = False
        self.unknown = False
        self.batches: list[str] = []

    def event(self, kind: RunEventType, data: dict[str, object]) -> RunEvent:
        event = RunEvent(self.run.id, len(self.events) + 1, kind, 1, data, self.run.created_at)
        self.events.append(event)
        return event

    async def get_run(self, run_id: str) -> RunSnapshot:
        return self.run

    async def append_run_event(
        self, run_id: str, event_type: RunEventType, data: dict[str, object], **kwargs: object
    ) -> RunEvent:
        return self.event(event_type, data)

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        return list(self.calls)

    async def list_tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        return list(self.tools)

    async def read_research_task_calls(self, run_id: str, task_id: str) -> list[ModelCallRecord]:
        return [call for call in self.calls if call.task_id == task_id]

    async def read_research_task_messages(
        self, run_id: str, task_id: str
    ) -> list[RunMessageRecord]:
        return list(self.messages)

    async def read_research(self, run_id: str) -> ResearchSnapshot:
        return ResearchSnapshot(
            run_id,
            self.run.session_id,
            str(uuid4()),
            ResearchPhase.RESEARCHING,
            "Compare source evidence",
            0,
            {},
            (self.task,),
            None,
            600,
        )

    async def reserve_research_call(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        operation_id: str,
        request_snapshot: dict[str, object],
        reserved_tokens: int,
        reserved_cost: float,
        **kwargs: object,
    ) -> tuple[object, ModelCallRecord]:
        if self.unknown:
            raise ApplicationError("research_usage_unknown", "Usage is unknown")
        call = ModelCallRecord(
            str(uuid4()),
            run_id,
            len(self.calls) + 1,
            self.run.provider_id,
            self.run.model_id,
            self.run.adapter_kind,
            ModelCallStatus.PENDING,
            request_snapshot,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            self.run.created_at,
            None,
            task_id=task_id,
        )
        self.calls.append(call)
        self.task = replace(self.task, status=ResearchTaskStatus.RUNNING)
        return object(), call

    async def update_model_call(
        self, call_id: str, next_status: ModelCallStatus, **values: object
    ) -> ModelCallRecord:
        call = next(call for call in self.calls if call.id == call_id)
        filtered = {
            key: value
            for key, value in values.items()
            if key in ModelCallRecord.__dataclass_fields__
        }
        updated = replace(call, status=next_status, **filtered)
        self.calls[self.calls.index(call)] = updated
        return updated

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
        call = await self.update_model_call(call_id, ModelCallStatus.COMPLETED, **completion)
        created: list[ToolCallRecord] = []
        if assistant_message is not None:
            message = RunMessageRecord(
                str(uuid4()),
                run_id,
                len(self.messages) + 1,
                "assistant",
                assistant_message.content,
                call_id,
                None,
                self.run.created_at,
                assistant_message.tool_calls,
                assistant_message.continuation_items,
                task_id,
            )
            self.messages.append(message)
            for index, request in enumerate(assistant_message.tool_calls):
                tool = ToolCallRecord(
                    str(uuid4()),
                    run_id,
                    message.id,
                    index,
                    request.id,
                    request.name,
                    request.arguments,
                    ToolCallStatus.PENDING,
                    None,
                    None,
                    self.run.created_at,
                    None,
                    task_id=task_id,
                )
                self.tools.append(tool)
                created.append(tool)
        else:
            self.task = replace(
                self.task, status=ResearchTaskStatus.COMPLETED, result=terminal_result
            )
        return call, tuple(created), ()

    async def begin_tool_execution(
        self, run_id: str, call_id: str, **values: object
    ) -> tuple[ToolCallRecord, RunEvent]:
        tool = next(tool for tool in self.tools if tool.id == call_id)
        updated = replace(
            tool,
            status=ToolCallStatus.RUNNING,
            dispatch_id=cast(str, values["dispatch_id"]),
            dispatch_attempt=tool.dispatch_attempt + 1,
            dispatch_generation=self.run.generation,
            recovery_policy=cast(str, values["recovery_policy"]),
        )
        self.tools[self.tools.index(tool)] = updated
        return updated, self.event(RunEventType.PHASE, {"phase": "web_read"})

    async def record_tool_result(
        self, run_id: str, call_id: str, content: str, *, status: ToolCallStatus, **values: object
    ) -> tuple[RunMessageRecord, ToolCallRecord, RunEvent]:
        tool = next(tool for tool in self.tools if tool.id == call_id)
        updated = replace(tool, status=status)
        self.tools[self.tools.index(tool)] = updated
        message = RunMessageRecord(
            str(uuid4()),
            run_id,
            len(self.messages) + 1,
            "tool",
            content,
            None,
            call_id,
            self.run.created_at,
            task_id=tool.task_id,
        )
        self.messages.append(message)
        event = self.event(RunEventType.TOOL_RESULT, {"tool_call_id": call_id})
        if (
            self.fail_after_tenth_source
            and len([item for item in self.tools if item.status is ToolCallStatus.COMPLETED]) == 10
        ):
            self.fail_after_tenth_source = False
            raise ApplicationError("database_unavailable", "Controlled receipt loss")
        return message, updated, event

    async def mark_research_unknown(
        self, run_id: str, generation: int, call_id: str, error_code: str = "model_outcome_unknown"
    ) -> None:
        self.unknown = True
        call = next(call for call in self.calls if call.id == call_id)
        self.calls[self.calls.index(call)] = replace(call, error_code=error_code)

    async def checkpoint_research(
        self,
        run_id: str,
        generation: int,
        task_id: str,
        expected_revision: int,
        operation_id: str,
        checkpoint: dict[str, object],
    ) -> ResearchTask:
        self.task = replace(
            self.task,
            checkpoint=checkpoint,
            revision=self.task.revision + 1,
            status=ResearchTaskStatus.FAILED if checkpoint.get("stop_reason") else self.task.status,
        )
        return self.task

    async def append_assistant_batch(
        self, run_id: str, call_id: str, batch_index: int, text: str
    ) -> None:
        self.batches.append(text)
        raise AssertionError("Research worker text cannot be published")


class ReadArguments(BaseModel):
    url: str


class RecordedPages:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def execute(
        self, name: str, arguments: dict[str, object], context: object
    ) -> tuple[str, bool]:
        self.urls.append(str(arguments["url"]))
        return json.dumps(
            {"source_id": str(uuid4()), "status": "success", "lines": ["Saved supporting evidence"]}
        ), False


class SourceResearcher:
    def __init__(self, *, lost_response: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.lost_response = lost_response

    async def stream(self, request: ModelRequest, api_key: str, user_id: str):
        self.requests.append(request)
        yield ModelStreamEvent("text_delta", text="Private research notes")
        if self.lost_response:
            raise ProviderError("provider_connection", "Controlled lost response")
        if len(self.requests) == 1:
            for index in range(10):
                yield ModelStreamEvent(
                    "tool_call",
                    tool_call=ModelToolCall(
                        f"read-{index}", "web_read", {"url": f"https://example.com/{index}"}
                    ),
                )
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=20, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent(
            "completed", finish_kind="tool" if len(self.requests) == 1 else "final"
        )


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, *, lost_response: bool = False):
        run = make_run()
        records = MemoryResearch(run)
        provider = SourceResearcher(lost_response=lost_response)
        pages = RecordedPages()
        tools = ToolRegistry(
            additional_tools=(
                ToolRegistration(
                    "web_read",
                    "Read a source",
                    ReadArguments,
                    pages,
                    recovery_policy=ToolRecoveryPolicy.CHECKPOINTED,
                    result_format="json",
                ),
            )
        )
        service = RunService(
            cast(RunRepository, records),
            cast(SessionRepository, object()),
            cast(SettingsRepository, object()),
            cast(ProfileRepository, object()),
            cast(SecretStorePort, object()),
            {},
            tool_registry=tools,
            research_repository=cast(ResearchRepository, records),
        )
        runtime = RunContext(
            cast(RunRepository, records),
            cast(SessionRepository, object()),
            cast(ContextCheckpointRepository, records),
            service._budget_totals,
            service._publish,
            COMMON_SECRET_REDACTOR,
        )
        model = ModelDescriptor(
            run.model_id,
            "openai",
            "OpenAI",
            run.adapter_kind,
            "gpt-5.5",
            "GPT-5.5",
            False,
            True,
            True,
            True,
        )
        execution = _ExecutionContext(
            model,
            cast(StreamingProviderAdapter, provider),
            "",
            str(uuid4()),
            None,
            None,
            tools.specs(),
            runtime,
        )

        async def unused(task: ResearchTask) -> ResearchGeneration | None:
            raise AssertionError("No nested delegation")

        coordinator = ResearchCoordinator(cast(ResearchRepository, records), run, unused)
        return run, records, provider, pages, service, execution, coordinator

    async def test_recovery_keeps_ten_recorded_pages_and_worker_text_private(self) -> None:
        run, records, provider, pages, service, execution, coordinator = self.harness()
        records.fail_after_tenth_source = True
        with self.assertRaises(ApplicationError) as fault:
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(fault.exception.code, "database_unavailable")
        self.assertEqual(len(pages.urls), 10)
        self.assertEqual(
            validate_task_recovery(run, (records.task,), records.calls, records.tools), ()
        )
        result = await service._run_research_task(run, execution, coordinator, records.task)
        self.assertIsNotNone(result)
        self.assertEqual(len(pages.urls), 10)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(records.batches, [])
        self.assertEqual(records.task.status, ResearchTaskStatus.COMPLETED)
        self.assertEqual([call.step_index for call in records.calls], [1, 2])
        self.assertEqual(
            len([message for message in provider.requests[-1].messages if message.role == "tool"]),
            10,
        )

    async def test_lost_paid_response_freezes_further_model_work(self) -> None:
        run, records, provider, pages, service, execution, coordinator = self.harness(
            lost_response=True
        )
        with self.assertRaises(ProviderError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertTrue(records.unknown)
        self.assertEqual(
            len(validate_task_recovery(run, (records.task,), records.calls, records.tools)), 1
        )
        with self.assertRaises(ProviderError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(pages.urls, [])
        self.assertEqual(records.batches, [])

    async def test_cancel_closes_provider_read_and_retains_uncertain_usage(self) -> None:
        run, records, _provider, _pages, service, execution, coordinator = self.harness()
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingProvider:
            async def stream(self, request: ModelRequest, api_key: str, user_id: str):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield ModelStreamEvent("text_delta", text="Private notes")
                finally:
                    closed.set()

        execution = replace(execution, provider=cast(StreamingProviderAdapter, WaitingProvider()))
        active = asyncio.create_task(
            service._run_research_task(run, execution, coordinator, records.task)
        )
        await asyncio.wait_for(entered.wait(), 1)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertTrue(closed.is_set())
        self.assertTrue(records.unknown)
        self.assertEqual(records.batches, [])

    async def test_other_branch_can_advance_while_a_tool_exchange_is_pending(self) -> None:
        run, records, _provider, _pages, _service, _execution, _coordinator = self.harness()
        first = {
            "purpose": "generation",
            "research": {"version": "bounded-research-v1", "task_id": records.task.id},
        }
        await records.reserve_research_call(
            run.id, run.generation, records.task.id, str(uuid4()), first, 100, 0.01
        )
        await records.commit_research_generation(
            run.id,
            run.generation,
            records.task.id,
            records.calls[0].id,
            {},
            assistant_message=ModelMessage(
                "assistant",
                "Finding sources",
                tool_calls=(ModelToolCall("read-a", "web_read", {"url": "https://example.com"}),),
            ),
        )
        other = replace(records.task, id=str(uuid4()), objective="Independent question")
        second = {
            "purpose": "generation",
            "research": {"version": "bounded-research-v1", "task_id": other.id},
        }
        await records.reserve_research_call(
            run.id, run.generation, other.id, str(uuid4()), second, 100, 0.01
        )
        self.assertEqual(
            validate_task_recovery(run, (records.task, other), records.calls, records.tools), ()
        )

    async def test_collection_scope_preserves_root_deadline_and_report_time(self) -> None:
        run = make_run()
        scope = RunService._research_execution_scope(run, finalization=False)
        root_deadline = datetime.fromisoformat(run.deadline_at)
        self.assertEqual(
            (root_deadline - datetime.fromisoformat(scope.deadline_at)).total_seconds(), 150
        )
        self.assertEqual(run.deadline_at, root_deadline.isoformat())
        self.assertIs(RunService._research_execution_scope(run, finalization=True), run)

    async def test_expired_collection_cutoff_starts_no_paid_call(self) -> None:
        run, records, provider, _pages, service, execution, coordinator = self.harness()
        run = replace(run, deadline_at=(datetime.now(UTC) + timedelta(seconds=149)).isoformat())
        records.run = run
        result = await service._run_research_task(run, execution, coordinator, records.task)
        self.assertIsNone(result)
        self.assertEqual(records.calls, [])
        self.assertEqual(provider.requests, [])
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_finalization_reserve")
        self.assertEqual(records.run.deadline_at, run.deadline_at)

    async def test_tool_cutoff_closes_current_and_pending_records_without_unknown_usage(
        self,
    ) -> None:
        run, records, provider, _pages, service, execution, coordinator = self.harness()
        cutoff = RunService._research_execution_scope(run, finalization=False)
        expired = False
        deadlines: list[datetime] = []
        actual_remaining = service._remaining_seconds

        class DeadlineTool:
            async def execute(
                self, name: str, arguments: dict[str, object], context: ToolExecutionContext
            ) -> tuple[str, bool]:
                nonlocal expired
                deadlines.append(context.deadline_at)
                expired = True
                raise TimeoutError

        tools = ToolRegistry(
            additional_tools=(
                ToolRegistration(
                    "web_read",
                    "Read a source",
                    ReadArguments,
                    DeadlineTool(),
                    recovery_policy=ToolRecoveryPolicy.CHECKPOINTED,
                    result_format="json",
                ),
            )
        )
        service._tool_registry = tools
        execution = replace(execution, tools=tools.specs())

        def remaining(snapshot: RunSnapshot) -> float:
            if expired and snapshot.deadline_at == cutoff.deadline_at:
                return 0
            return actual_remaining(snapshot)

        with patch.object(service, "_remaining_seconds", side_effect=remaining):
            result = await service._run_research_task(run, execution, coordinator, records.task)
        self.assertIsNone(result)
        self.assertEqual(deadlines, [datetime.fromisoformat(cutoff.deadline_at)])
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(records.tools[0].status, ToolCallStatus.TIMED_OUT)
        self.assertTrue(all(tool.status is ToolCallStatus.CANCELLED for tool in records.tools[1:]))
        self.assertFalse(records.unknown)
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_finalization_reserve")
        self.assertEqual(records.run.deadline_at, run.deadline_at)


if __name__ == "__main__":
    unittest.main()


class RepositoryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_reads_keep_research_mode_and_task_scope(self) -> None:
        from dataclasses import asdict
        from uuid import UUID

        import httpx

        from app.core.config import Settings
        from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

        run = make_run()
        memory = MemoryResearch(run)
        task_id = memory.task.id
        user = uuid4()
        await memory.reserve_research_call(
            run.id, run.generation, task_id, str(uuid4()), {}, 100, 0.01
        )
        await memory.commit_research_generation(
            run.id,
            run.generation,
            task_id,
            memory.calls[0].id,
            {},
            assistant_message=ModelMessage(
                "assistant",
                "notes",
                tool_calls=(ModelToolCall("read-1", "web_read", {"url": "https://example.com"}),),
            ),
        )
        run_row = asdict(run)
        run_row["chat_id"] = run_row.pop("session_id")
        call_row = asdict(memory.calls[0])
        call_row["cached_read_tokens"] = call_row.pop("cached_tokens")
        message_row = asdict(memory.messages[0])
        message_row["continuation"] = []
        tool_row = asdict(memory.tools[0])
        rows = {
            "runs": run_row,
            "model_calls": call_row,
            "run_messages": message_row,
            "tool_calls": tool_row,
        }
        for row in rows.values():
            row["user_id"] = str(user)

        async def handle(request: httpx.Request) -> httpx.Response:
            table = request.url.path.rsplit("/", 1)[-1]
            selected = request.url.params["select"].split(",")
            self.assertIn("run_mode" if table == "runs" else "task_id", selected)
            self.assertEqual(request.url.params["user_id"], f"eq.{user}")
            return httpx.Response(
                200, json=[{key: value for key, value in rows[table].items() if key in selected}]
            )

        settings = Settings(
            _env_file=None,
            supabase_url="https://fixture.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        )
        tokens = VerifiedTokenSource(UUID(str(user)))
        tokens.record_verified("fixture-token", user, datetime.now(UTC) + timedelta(hours=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            repository = SupabaseRepository(settings, user, tokens, client)
            restored = await repository.get_run(run.id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.run_mode, "research")
            self.assertEqual((await repository.list_model_calls(run.id))[0].task_id, task_id)
            self.assertEqual((await repository.list_tool_calls(run.id))[0].task_id, task_id)
            self.assertEqual((await repository.list_run_messages(run.id))[0].task_id, task_id)
