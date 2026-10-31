import asyncio
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx

from app.application.errors import ApplicationError
from app.application.ports import SecretStorePort, StreamingProviderAdapter
from app.application.recovery_contracts import RecoveryContracts
from app.application.research_reports import ResearchReportService
from app.application.research_task_context import project_research_task_context
from app.application.runs import RunService
from app.application.tools import ToolRegistry
from app.application.web_evidence import WebEvidenceService
from app.application.web_tools import WebToolService
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.retrieval import ExtractedDocumentResult
from app.domain.runtime import (
    ModelCallStatus,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEventType,
    RunStatus,
)
from app.domain.web import SourceStatus, WebUsage
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from tests import test_research_postgres as database_tests
from tests.test_research_execution_postgres import FixtureSecrets, PostgresTransport
from tests.test_research_handoff_runtime import QUESTION

FACT = "The product uses a shared request queue."


class DurablePages:
    def __init__(self):
        self.urls = []

    async def is_ready(self) -> bool:
        return True

    async def read_document(self, request, context):
        self.urls.append(request.url)
        text = FACT + "\nEach request is assigned to one worker."
        return ExtractedDocumentResult(
            SourceStatus.SUCCESS,
            request.url,
            request.url,
            "text/plain",
            text,
            WebUsage(
                page_attempts=1,
                network_requests=1,
                response_bytes=len(text),
                extracted_bytes=len(text),
            ),
            title="Documented product behavior",
            extractor_version="fixture-v2",
        )


class DurableTransport(PostgresTransport):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.fail_after_ten = True

    async def __call__(self, request):
        response = await super().__call__(request)
        if request.url.path.endswith("/rpc/mutate"):
            payload = json.loads(request.content)
            if payload.get("p_action") == "record_tool_result":
                receipt = response.json()
                if receipt.get("tool_call", {}).get("name") == "web_read":
                    count = await asyncio.to_thread(
                        self.fixture.sql,
                        "SELECT count(*) FROM trellis.web_sources WHERE run_id="
                        + database_tests.literal(self.fixture.run_id)
                        + ";",
                        authenticated=False,
                    )
                    if self.fail_after_ten and int(count.stdout) == 10:
                        self.failed = True
                        raise ApplicationError(
                            "database_unavailable", "Controlled failure after ten saved pages"
                        )
        return response


class DurableResearcher:
    def __init__(self):
        self.requests = []

    async def stream(self, request: ModelRequest, api_key, user_id):
        self.requests.append(request)
        prompt = request.messages[0].content
        calls = []
        text = "Saved research findings are ready."
        if not request.tools:
            data = json.loads(
                prompt[prompt.index('{"report":') :]
                if '{"report":' in prompt
                else prompt.rsplit("\n", 1)[-1]
            )
            if "report" in data:
                text = json.dumps(
                    {
                        "accepted_claim_ids": [claim["id"] for claim in data["report"]["claims"]],
                        "issues": [],
                        "question_coverage": [
                            {
                                "question_id": "comparison",
                                "requirement_ids": ["behavior"],
                                "claim_ids": ["summary"],
                            }
                        ],
                    }
                )
            else:
                source = data["saved_evidence"]["sources"][0]["source_id"]
                text = json.dumps(
                    {
                        "markdown": (
                            f"## Summary\n\n{FACT} [[source:{source}:L1-L1]]\n\n## Findings\n\n"
                            "The collected evidence answers the requested behavior question."
                            "\n\n## Conflicts\n\nNo conflicting finding was recorded."
                            "\n\n## Limitations\n\nThis bounded fixture verifies recovery "
                            "and citation publication."
                        ),
                        "claims": [
                            {
                                "id": "summary",
                                "text": FACT,
                                "citations": [
                                    {"source_id": source, "start_line": 1, "end_line": 1}
                                ],
                            }
                        ],
                        "covered_question_ids": ["comparison"],
                    }
                )
        else:
            state = json.loads(prompt)
            notes = state["checkpoint"]
            if state.get("checkpoint_required"):
                calls = [
                    ModelToolCall(
                        str(uuid4()),
                        "research_checkpoint",
                        {
                            "findings": [
                                {
                                    "id": read["source_id"],
                                    "text": FACT,
                                    "question_ids": ["comparison"],
                                    "requirement_ids": ["behavior"],
                                    "citations": [
                                        {
                                            "source_id": read["source_id"],
                                            "start_line": 1,
                                            "end_line": 1,
                                        }
                                    ],
                                }
                                for read in state["unprocessed_reads"]
                            ]
                        },
                    )
                ]
            elif any(tool.name == "delegate_research" for tool in request.tools):
                if not notes.get("subquestions") and not state.get("research_brief", {}).get(
                    "subquestions"
                ):
                    calls = [
                        ModelToolCall(
                            str(uuid4()),
                            "research_checkpoint",
                            {
                                "brief": {
                                    "scope": "Compare documented product behavior",
                                    "expected_output": "A supported summary",
                                },
                                "subquestions": [QUESTION],
                            },
                        )
                    ]
                elif not state.get("worker_findings"):
                    calls = [
                        ModelToolCall(
                            str(uuid4()),
                            "delegate_research",
                            {
                                "round_index": 1,
                                "assignments": [
                                    {
                                        "objective": (
                                            "Read ten pages and save cited behavior findings"
                                        ),
                                        "question_ids": ["comparison"],
                                        "requirement_ids": ["behavior"],
                                        "purpose": "fill_gap",
                                        "expected_value": "Verify documented behavior",
                                    }
                                ],
                            },
                        )
                    ]
                else:
                    notebooks = [notes, *state["worker_findings"]]
                    covered = {
                        finding["id"]
                        for notebook in notebooks
                        for finding in notebook.get("findings", [])
                    }
                    sources = dict.fromkeys(
                        source
                        for notebook in notebooks
                        for source in notebook.get("examined_source_ids", [])
                    )
                    calls = [
                        ModelToolCall(str(uuid4()), "web_source_read", {"source_id": source})
                        for source in list(sources.keys() - covered)[:2]
                    ]
            elif not notes.get("examined_source_ids"):
                discovered = [
                    page["source_id"]
                    for message in request.messages
                    if message.role == "tool"
                    for page in json.loads(message.content).get("pages", [])
                ]
                calls = (
                    [
                        ModelToolCall(
                            str(uuid4()), "research_checkpoint", {"examined_source_ids": discovered}
                        )
                    ]
                    if discovered
                    else [
                        ModelToolCall(
                            str(uuid4()),
                            "web_read",
                            {
                                "urls": [
                                    f"https://example.com/{index}"
                                    for index in range(start, start + 5)
                                ]
                            },
                        )
                        for start in (0, 5)
                    ]
                )
            else:
                covered = {finding["id"] for finding in notes.get("findings", [])}
                unread = [
                    source for source in notes["examined_source_ids"] if source not in covered
                ]
                calls = [
                    ModelToolCall(str(uuid4()), "web_source_read", {"source_id": source})
                    for source in unread[:2]
                ]
        yield ModelStreamEvent("text_delta", text=text)
        for call in calls:
            yield ModelStreamEvent("tool_call", tool_call=call)
        yield ModelStreamEvent(
            "usage", input_tokens=200, output_tokens=100, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent("completed", finish_kind="tool" if calls else "final")


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchV2ExecutionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = database_tests.ResearchPostgresTests()
        await asyncio.to_thread(self.fixture.setUp)
        self.settings = Settings(
            _env_file=None,
            supabase_url="https://fixture.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        )
        owner = UUID(self.fixture.owner)
        self.tokens = VerifiedTokenSource(owner)
        self.tokens.record_verified("fixture-token", owner, datetime.now(UTC) + timedelta(hours=1))
        self.transport = DurableTransport(self.fixture)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        self.repository = SupabaseRepository(self.settings, owner, self.tokens, self.client)
        self.installation_id = str(uuid4())
        self.service, self.provider, self.pages, self.contracts = self.make_service(self.repository)
        run = await self.repository.get_run(self.fixture.run_id)
        assert run is not None
        self.repository._record_lease(run, self.fixture.capability)
        session = await self.repository.get_session(run.session_id)
        assert session is not None
        model = next(
            model for model in await self.repository.list_models() if model.id == run.model_id
        )
        request = str(uuid4())
        contract = await self.contracts.create(session, request, model)
        await self.repository.transition_run_record(
            run.id, RunStatus.FAILED, RunEventType.FAILED, {"code": "fixture_retired"}
        )
        created = await self.repository.create_run(
            session.id,
            str(uuid4()),
            request,
            "Compare documented product behavior",
            model,
            recovery_contract=contract,
            run_mode="research",
        )
        self.fixture.run_id = created.id
        self.fixture.capability = self.repository._run_capability(created.id)
        await asyncio.to_thread(
            self.fixture.sql,
            "BEGIN; ALTER TABLE trellis.runs DISABLE TRIGGER preserve_research_profile; "
            "UPDATE trellis.runs SET research_policy_version='bounded-research-v2' WHERE id="
            + database_tests.literal(created.id)
            + "; ALTER TABLE trellis.runs ENABLE TRIGGER preserve_research_profile; COMMIT;",
            authenticated=False,
        )
        execution_run = await self.repository.get_run(created.id)
        assert execution_run is not None
        self.execution_run = execution_run
        self.assertEqual(self.execution_run.research_policy_version, "bounded-research-v2")
        self.assertEqual(self.execution_run.execution_budget_seconds, 600)

    def make_service(self, repository):
        capabilities = RuntimeCapabilities(frozenset({"web_read"}))
        pages = DurablePages()
        web = WebToolService(
            WebEvidenceService(repository, repository, owner_id=self.fixture.owner), retriever=pages
        )
        registry = ToolRegistry(
            capabilities=capabilities,
            additional_tools=web.registrations(
                search_ready=False, read_ready=True, research_ready=True
            ),
        )
        provider = DurableResearcher()
        contracts = RecoveryContracts(
            self.installation_id,
            repository,
            registry,
            None,
            capabilities,
            frozenset({"openai", "anthropic"}),
        )
        service = RunService(
            repository,
            repository,
            repository,
            repository,
            cast(SecretStorePort, FixtureSecrets()),
            {
                "openai": cast(StreamingProviderAdapter, provider),
                "anthropic": cast(StreamingProviderAdapter, provider),
            },
            tool_registry=registry,
            lease_repository=repository,
            capabilities=capabilities,
            research_repository=repository,
            research_reports=ResearchReportService(repository, repository),
            recovery_repository=repository,
            recovery_compatibility=contracts.compatibility,
        )
        return service, provider, pages, contracts

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        await asyncio.to_thread(self.fixture.tearDown)

    async def test_v2_profile_survives_direct_latest_and_reconnected_reads(self):
        direct = await self.repository.get_run(self.execution_run.id)
        latest = await self.repository.get_latest_run_for_session(self.execution_run.session_id)
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.transport)) as client:
            reconnected = SupabaseRepository(
                self.settings, UUID(self.fixture.owner), self.tokens, client
            )
            restored = await reconnected.get_run(self.execution_run.id)
        for run in (direct, latest, restored):
            assert run is not None
            self.assertEqual(run.research_policy_version, "bounded-research-v2")
            self.assertEqual(run.execution_budget_seconds, 600)
            self.assertEqual(run.deadline_at, self.execution_run.deadline_at)

    async def test_takeover_after_ten_saved_pages_resumes_notes_and_publishes_once(self):
        await self.service._execute_snapshot(self.execution_run)
        self.assertTrue(self.transport.failed)
        self.assertEqual(len(self.pages.urls), 10)
        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.RUNNING, saved.error_message)
        self.assertFalse(self.repository.owns_run_lease(saved.id))
        calls = await self.repository.list_model_calls(saved.id)
        self.assertTrue(all(call.status is ModelCallStatus.COMPLETED for call in calls))
        self.assertEqual(
            len((await self.repository.list_web_sources(saved.session_id, 0, 20)).items), 10
        )
        self.assertEqual(
            sum(
                message.role == "assistant"
                for message in await self.repository.list_messages(saved.session_id)
            ),
            0,
        )

        transport = DurableTransport(self.fixture)
        transport.fail_after_ten = False
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            repository = SupabaseRepository(
                self.settings, UUID(self.fixture.owner), self.tokens, client
            )
            service, provider, pages, _contracts = self.make_service(repository)
            try:
                claimed, reason = await repository.claim_recovery(saved, self.installation_id)
                self.assertIsNone(reason)
                self.assertEqual(claimed.generation, saved.generation + 1)
                self.assertEqual(claimed.deadline_at, self.execution_run.deadline_at)
                self.assertEqual(claimed.max_total_tokens, self.execution_run.max_total_tokens)
                stale = await asyncio.to_thread(
                    self.fixture.rpc,
                    "set_handoff",
                    {
                        "run_id": saved.id,
                        "generation": saved.generation,
                        "task_id": str(uuid4()),
                        "reserved_tokens": 1000,
                        "reserved_cost": 0.01,
                    },
                    check=False,
                )
                self.assertNotEqual(stale.returncode, 0)
                self.assertIn("capability invalid or expired", stale.stderr)
                await service._execute_recovery(claimed)
                completed = await repository.get_run(saved.id)
                assert completed is not None
                self.assertEqual(completed.status, RunStatus.COMPLETED, completed.error_message)
                state = await repository.read_research(saved.id)
                assert state is not None
                self.assertEqual(state.phase.value, "completed")
                self.assertEqual(pages.urls, [])
                self.assertEqual(len(state.source_reads), 10)
                worker = next(task for task in state.tasks if task.role == "worker")
                self.assertEqual(worker.checkpoint["stop_reason"], "research_worker_burst_complete")
                self.assertEqual(len(cast(list, worker.checkpoint["findings"])), 6)
                lead = next(task for task in state.tasks if task.role == "lead")
                self.assertEqual(len(cast(list, lead.checkpoint["findings"])), 4)
                self.assertEqual(
                    sum(
                        len(cast(list, task.checkpoint.get("findings", [])))
                        for task in state.tasks
                        if task.role in {"lead", "worker"}
                    ),
                    10,
                )
                messages = await repository.read_research_task_messages(saved.id, worker.id)
                task_calls = await repository.read_research_task_calls(saved.id, worker.id)
                projection = project_research_task_context(
                    saved.id,
                    worker.id,
                    messages,
                    await repository.list_tool_calls(saved.id),
                    task_calls,
                )
                self.assertEqual(projection.unprocessed_reads, ())
                self.assertGreater(projection.checkpointed_read_through_ordinal, 0)
                self.assertEqual(
                    len(
                        [
                            message
                            for message in await repository.list_messages(saved.session_id)
                            if message.role == "assistant"
                        ]
                    ),
                    1,
                )
                assert state.report_model_call_id is not None
                self.assertEqual(
                    len(
                        await repository.read_web_citations(
                            saved.session_id, state.report_model_call_id
                        )
                    ),
                    1,
                )
                self.assertEqual(len(state.reviewed_coverage), 1)
                self.assertTrue(any(not request.tools for request in provider.requests))
            finally:
                await service.close()


if __name__ == "__main__":
    unittest.main()
