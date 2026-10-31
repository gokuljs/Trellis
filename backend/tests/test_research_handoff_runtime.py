import json
import unittest
from dataclasses import replace
from typing import cast
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
from app.application.research import ResearchCoordinator, validate_task_recovery
from app.application.research_ports import ResearchRepository
from app.application.research_task_context import project_research_task_context
from app.application.run_context import RunContext
from app.application.runs import RunService, _ExecutionContext
from app.application.secret_redaction import COMMON_SECRET_REDACTOR
from app.application.tools import ToolRegistration, ToolRegistry
from app.domain.models import ModelDescriptor
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.research import (
    RESEARCH_POLICY_V2,
    ResearchPhase,
    ResearchSnapshot,
    ResearchTaskStatus,
)
from app.domain.runtime import ModelRequest, ModelStreamEvent, ModelToolCall
from tests.test_research_agent_loop import MemoryResearch, make_run

QUESTION = {
    "id": "comparison",
    "question": "Compare documented product behavior",
    "evidence_requirements": [
        {"id": "behavior", "description": "Read the documented behavior", "depth": "source_excerpt"}
    ],
}


class HandoffMemory(MemoryResearch):
    def __init__(self, run):
        super().__init__(run)
        self.holds = {}
        self.handoff_calls = []
        self.fail_body_receipt = False
        self.deny_holds = False

    async def read_research(self, run_id):
        return ResearchSnapshot(
            run_id,
            self.run.session_id,
            str(uuid4()),
            ResearchPhase.RESEARCHING,
            "Compare documented product behavior",
            0,
            {"subquestions": [QUESTION]},
            (self.task,),
            None,
            600,
            policy_version=RESEARCH_POLICY_V2,
            execution_budget_seconds=600,
        )

    async def research_budget_state(self, run_id, generation):
        return {
            "used": {"model_calls": len(self.calls), "tokens": 0, "estimated_cost": 0},
            "held": {"model_calls": len(self.holds)},
            "collection_remaining": {"model_calls": 60, "tokens": 200_000, "estimated_cost": 4},
        }

    async def set_research_handoff(
        self, run_id, generation, task_id, reserved_tokens, reserved_cost
    ):
        if self.deny_holds:
            raise ApplicationError("research_finalization_reserve", "No collection capacity")
        self.holds[task_id] = reserved_tokens, reserved_cost
        return await self.research_budget_state(run_id, generation)

    async def release_research_handoff(self, run_id, generation, task_id):
        self.holds.pop(task_id, None)

    async def reserve_research_call(self, *args, **kwargs):
        if kwargs.get("handoff"):
            self.holds.pop(args[2], None)
            self.handoff_calls.append(len(self.calls) + 1)
        return await super().reserve_research_call(*args, **kwargs)

    async def record_tool_result(self, *args, **kwargs):
        result = await super().record_tool_result(*args, **kwargs)
        if self.fail_body_receipt and json.loads(result[0].content).get("substantive") is True:
            self.fail_body_receipt = False
            raise ApplicationError("database_unavailable", "Controlled saved read receipt loss")
        return result


class DiscoverArguments(BaseModel):
    url: str


class SavedArguments(BaseModel):
    source_id: str


class SavedPages:
    def __init__(self):
        self.sources = {}
        self.fetches = []
        self.body_reads = []

    async def execute(self, name, arguments, context):
        if name == "web_read":
            url = arguments["url"]
            self.fetches.append(url)
            identity = self.sources.setdefault(url, str(uuid4()))
            return json.dumps({"status": "success", "source_id": identity}), False
        identity = arguments["source_id"]
        if identity not in self.sources.values():
            raise AssertionError("Only saved pages may be read")
        self.body_reads.append(identity)
        return json.dumps(
            {
                "status": "success",
                "source_id": identity,
                "content_hash": "a" * 64,
                "substantive": True,
                "read_kind": "source_lines",
                "presented_ranges": [{"start_line": 1, "end_line": 1}],
                "untrusted_lines": [{"line": 1, "text": "Documented product behavior"}],
            }
        ), False


class CheckpointResearcher:
    def __init__(self, *, overread=False, lost_response=False, empty_checkpoint=False):
        self.requests = []
        self.overread = overread
        self.lost_response = lost_response
        self.empty_checkpoint = empty_checkpoint

    async def stream(self, request: ModelRequest, api_key, user_id):
        self.requests.append(request)
        if self.lost_response:
            raise ProviderError("provider_connection", "Controlled response loss")
        state = json.loads(request.messages[0].content)
        notes = state["checkpoint"]
        sources = notes.get("examined_source_ids", [])
        calls = []
        if state.get("checkpoint_required"):
            reads = state["unprocessed_reads"]
            calls = [
                ModelToolCall(
                    str(uuid4()),
                    "research_checkpoint",
                    {
                        "findings": [
                            {
                                "id": read["source_id"],
                                "text": "The source documents product behavior.",
                                "question_ids": ["comparison"],
                                "requirement_ids": ["behavior"],
                                "citations": [
                                    {"source_id": read["source_id"], "start_line": 1, "end_line": 1}
                                ],
                            }
                            for read in reads
                        ],
                    },
                )
            ]
            if self.empty_checkpoint:
                calls = [ModelToolCall(str(uuid4()), "research_checkpoint", {})]
        elif not sources:
            discovered = [
                json.loads(message.content)["source_id"]
                for message in request.messages
                if message.role == "tool" and "source_id" in json.loads(message.content)
            ]
            calls = (
                [
                    ModelToolCall(
                        str(uuid4()), "research_checkpoint", {"examined_source_ids": discovered}
                    )
                ]
                if discovered
                else [
                    ModelToolCall(str(uuid4()), "web_read", {"url": f"https://example.com/{index}"})
                    for index in range(10)
                ]
            )
        else:
            covered = {finding["id"] for finding in notes.get("findings", [])}
            unread = [identity for identity in sources if identity not in covered]
            calls = [
                ModelToolCall(str(uuid4()), "web_source_read", {"source_id": identity})
                for identity in unread[: 3 if self.overread else 2]
            ]
        for call in calls:
            yield ModelStreamEvent("tool_call", tool_call=call)
        if not calls:
            yield ModelStreamEvent("text_delta", text="The saved findings cover this assignment.")
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=30, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent("completed", finish_kind="tool" if calls else "final")


class HandoffRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, **provider_options):
        run = replace(
            make_run(),
            research_policy_version=RESEARCH_POLICY_V2,
            execution_budget_seconds=600,
            max_model_calls=72,
            max_tool_calls=192,
            max_total_tokens=300_000,
            max_cost_usd=6,
        )
        records = HandoffMemory(run)
        provider = CheckpointResearcher(**provider_options)
        pages = SavedPages()
        registry = ToolRegistry(
            additional_tools=tuple(
                ToolRegistration(
                    name,
                    "Read saved research evidence",
                    arguments,
                    pages,
                    recovery_policy=ToolRecoveryPolicy.CHECKPOINTED,
                    result_format="json",
                )
                for name, arguments in (
                    ("web_read", DiscoverArguments),
                    ("web_source_read", SavedArguments),
                )
            )
        )
        service = RunService(
            cast(RunRepository, records),
            cast(SessionRepository, object()),
            cast(SettingsRepository, object()),
            cast(ProfileRepository, object()),
            cast(SecretStorePort, object()),
            {},
            tool_registry=registry,
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
            registry.specs(),
            runtime,
        )

        async def unused(task):
            raise AssertionError("No nested delegation")

        coordinator = ResearchCoordinator(cast(ResearchRepository, records), run, unused)
        return run, records, provider, pages, service, execution, coordinator

    async def test_ten_saved_pages_recover_without_refetch_and_findings_are_checkpointed(self):
        run, records, provider, pages, service, execution, coordinator = self.harness()
        records.fail_after_tenth_source = True
        with self.assertRaises(ApplicationError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(pages.fetches), 10)
        self.assertEqual(records.holds, {})
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(pages.fetches), 10)
        self.assertEqual(len(set(pages.fetches)), 10)
        self.assertEqual(len(pages.body_reads), 10)
        self.assertEqual(len(records.task.checkpoint["findings"]), 10)
        self.assertEqual(records.task.status, ResearchTaskStatus.COMPLETED)
        self.assertEqual(records.holds, {})
        self.assertEqual(len(records.handoff_calls), 5)
        for request in provider.requests:
            state = json.loads(request.messages[0].content)
            if state.get("checkpoint_required"):
                self.assertEqual({tool.name for tool in request.tools}, {"research_checkpoint"})
                self.assertLessEqual(len(state["unprocessed_reads"]), 2)
        last = provider.requests[-1]
        self.assertLess(len(last.messages), len(records.messages))
        self.assertEqual(
            validate_task_recovery(run, (records.task,), records.calls, records.tools), ()
        )

    async def test_a_batched_third_read_is_recorded_without_dispatch(self):
        run, records, _provider, pages, service, execution, coordinator = self.harness(
            overread=True
        )
        await service._run_research_task(run, execution, coordinator, records.task)
        blocked = [
            message
            for message in records.messages
            if message.role == "tool"
            and json.loads(message.content).get("status") == "checkpoint_required"
        ]
        self.assertTrue(blocked)
        self.assertEqual(len(pages.body_reads), 10)
        self.assertEqual(len(set(pages.body_reads)), 10)
        projection = project_research_task_context(
            run.id, records.task.id, records.messages, records.tools, records.calls
        )
        self.assertEqual(projection.unprocessed_reads, ())

    async def test_recovery_saves_pending_notes_when_more_collection_cannot_be_held(self):
        run, records, provider, pages, service, execution, coordinator = self.harness()
        records.fail_body_receipt = True
        with self.assertRaises(ApplicationError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(pages.body_reads), 1)
        self.assertEqual(records.holds, {})
        records.deny_holds = True
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(pages.body_reads), 1)
        self.assertEqual(len(records.task.checkpoint["findings"]), 1)
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_finalization_reserve")
        self.assertEqual(
            {tool.name for tool in provider.requests[-1].tools}, {"research_checkpoint"}
        )

    async def test_unknown_paid_outcome_releases_holds_and_stops_research(self):
        run, records, provider, pages, service, execution, coordinator = self.harness(
            lost_response=True
        )
        with self.assertRaises(ProviderError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertTrue(records.unknown)
        self.assertEqual(records.holds, {})
        self.assertEqual(pages.fetches, [])
        with self.assertRaises(ProviderError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), 1)

    async def test_invalid_checkpoints_get_one_durable_retry_without_consuming_reads(self):
        run, records, provider, pages, service, execution, coordinator = self.harness(
            empty_checkpoint=True
        )
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_checkpoint_invalid")
        self.assertEqual(len(pages.body_reads), 2)
        self.assertEqual(records.holds, {})
        projection = project_research_task_context(
            run.id, records.task.id, records.messages, records.tools, records.calls
        )
        self.assertEqual(len(projection.unprocessed_reads), 2)
        self.assertEqual(projection.checkpointed_read_through_ordinal, 0)
        attempted = [
            request
            for request in provider.requests
            if {tool.name for tool in request.tools} == {"research_checkpoint"}
        ]
        self.assertEqual(len(attempted), 2)
        self.assertTrue(all(call.estimated_cost is not None for call in records.calls))
        self.assertFalse(records.unknown)
        before = len(provider.requests)
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), before)

    async def test_shared_budget_numbers_remain_readable_after_credential_redaction(self):
        run, records, provider, _pages, service, execution, coordinator = self.harness()
        await service._run_research_task(run, execution, coordinator, records.task)
        state = json.loads(provider.requests[0].messages[0].content)
        budget = state["shared_budget"]
        self.assertEqual(budget["collection_remaining"]["model token count"], 200_000)
        self.assertEqual(budget["collection_remaining"]["estimated_cost"], 4)
        self.assertEqual(budget["remaining model token count"], 200_000)


if __name__ == "__main__":
    unittest.main()
