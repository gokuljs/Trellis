import asyncio
import json
import os
import re
import unittest
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from app.application.errors import ApplicationError
from app.application.ports import (
    SecretStorePort,
    StreamingProviderAdapter,
)
from app.application.recovery_contracts import RecoveryContracts
from app.application.research_reports import ResearchReportService
from app.application.run_context import RunContext
from app.application.runs import RunService
from app.application.tool_context import ToolExecutionContext
from app.application.tools import ToolRegistry
from app.application.web_evidence import WebEvidenceService
from app.application.web_tools import WebToolService
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.retrieval import ExtractedDocumentResult, RetrievalRequest
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEventType,
    RunStatus,
)
from app.domain.search import SearchHit, SearchResult, SearchStatus
from app.domain.web import SourceStatus, WebUsage
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from tests import test_research_postgres as database_tests


class PostgresTransport:
    def __init__(self, fixture: database_tests.ResearchPostgresTests) -> None:
        self.fixture = fixture
        self.fail_delegate_results = False
        self.failed = False
        self.delegate_tools: set[str] = set()

    @staticmethod
    def value(value: object) -> str:
        if isinstance(value, dict | list):
            return database_tests.literal(json.dumps(value)) + "::jsonb"
        if value is None:
            return "NULL"
        if type(value) is bool:
            return "TRUE" if value else "FALSE"
        if isinstance(value, int | float):
            return str(value)
        return database_tests.literal(str(value))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest/v1/")
        if path.startswith("rpc/"):
            function = path.removeprefix("rpc/")
            if re.fullmatch("[a-z_]+", function) is None:
                raise AssertionError("Unexpected RPC function")
            payload = json.loads(request.content)
            args = ",".join(name + "=>" + self.value(value) for name, value in payload.items())
            statement = (
                f"SELECT COALESCE(jsonb_agg(to_jsonb(t)),'[]') FROM trellis.{function}({args}) t;"
                if function == "list_chat_summaries"
                else f"SELECT trellis.{function}({args});"
            )
        else:
            if path not in {
                "runs",
                "messages",
                "model_calls",
                "tool_calls",
                "run_messages",
                "models",
                "profiles",
                "chats",
                "web_sources",
            }:
                raise AssertionError("Unexpected table " + path)
            columns = request.url.params["select"]
            if re.fullmatch("[a-z_,]+", columns) is None:
                raise AssertionError("Unexpected selected columns")
            filters = []
            order = ""
            for key, value in request.url.params.items():
                if key in {"select", "limit", "offset"}:
                    continue
                if key == "order":
                    if re.fullmatch("[a-z_.,]+", value) is None:
                        raise AssertionError("Unexpected order")
                    order = " ORDER BY " + ",".join(
                        item.replace(".", " ") for item in value.split(",")
                    )
                    continue
                if re.fullmatch("[a-z_]+", key) is None or not value.startswith("eq."):
                    raise AssertionError("Unexpected filter")
                filters.append(key + "=" + database_tests.literal(value[3:]))
            where = " WHERE " + " AND ".join(filters) if filters else ""
            limit = int(request.url.params.get("limit", "1000"))
            offset = int(request.url.params.get("offset", "0"))
            statement = (
                "SELECT COALESCE(jsonb_agg(to_jsonb(t)),'[]') FROM "
                f"(SELECT {columns} FROM trellis.{path}{where}{order} "
                f"LIMIT {limit} OFFSET {offset}) t;"
            )
        result = await asyncio.to_thread(self.fixture.sql, statement, check=False)
        if result.returncode:
            message = next(
                (
                    line.split("ERROR:", 1)[1].strip()
                    for line in result.stderr.splitlines()
                    if "ERROR:" in line
                ),
                result.stderr.strip(),
            )
            return httpx.Response(400, json={"code": "22023", "message": message})
        decoded = json.loads(result.stdout.strip() or "null")
        if path == "rpc/mutate":
            action = payload.get("p_action")
            if action == "begin_tool_execution" and isinstance(decoded, dict):
                tool = decoded.get("tool_call", {})
                if isinstance(tool, dict) and tool.get("name") == "delegate_research":
                    self.delegate_tools.add(str(tool["id"]))
            if (
                action == "record_tool_result"
                and self.fail_delegate_results
                and payload["p_payload"].get("tool_call_id") in self.delegate_tools
            ):
                self.failed = True
                raise ApplicationError(
                    "database_unavailable",
                    "Controlled worker failure after durable evidence collection.",
                )
        return httpx.Response(
            200,
            content=result.stdout.strip() or "null",
            headers={"Content-Type": "application/json"},
        )


class FixtureSecrets:
    async def get(self, provider: str) -> str:
        return "fixture-secret"


class FixturePages:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def is_ready(self) -> bool:
        return True

    async def read_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult:
        self.reads.append(request.url)
        if request.url.endswith("/a/archive"):
            text = "Archived A price is $8.\nA targets small businesses."
        elif "/a/" in request.url:
            text = "Current A price is $10.\nA targets small businesses."
        else:
            text = "B requires a sales quote.\nB targets enterprises."
        return ExtractedDocumentResult(
            SourceStatus.SUCCESS,
            request.url,
            request.url,
            "text/plain",
            text,
            WebUsage(
                page_attempts=1,
                network_requests=1,
                response_bytes=100,
                extracted_bytes=len(text.encode()),
            ),
            title=request.url,
            publisher="Fixture publisher",
            extractor_version="fixture-v1",
        )


class FixtureSearch:
    configured = True
    key_hash = "a" * 64

    async def search(
        self, query: str, max_results: int, context: ToolExecutionContext
    ) -> SearchResult:
        product = "a" if "Product A" in query else "b"
        return SearchResult(
            SearchStatus.SUCCESS,
            (
                SearchHit(
                    "Official pricing", f"https://example.com/{product}/current", "Discovery only"
                ),
            ),
            response_bytes=100,
        )


class FixtureCitation(TypedDict):
    source_id: str
    start_line: int
    end_line: int


class FixtureClaim(TypedDict):
    id: str
    text: str
    citations: list[FixtureCitation]


class ScriptedResearcher:
    def __init__(self) -> None:
        self.calls = 0
        self.workers_started: set[str] = set()
        self.both_workers = asyncio.Event()
        self.pages_per_worker: int | None = None
        self.use_saved_evidence = False
        self.checkpoint_stop_reason = False
        self.incomplete_draft = False
        self.draft_interrupted = False
        self.report_output_limits: list[int] = []

    async def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.calls += 1
        prompt = request.messages[0].content
        tool_names = {tool.name for tool in request.tools}
        exchanges = sum(message.role == "assistant" for message in request.messages)
        call: ModelToolCall | None = None
        text = "Internal research notes that must remain private."
        incomplete = False
        if not request.tools:
            self.report_output_limits.append(request.max_output_tokens)
            payload = json.loads(prompt.rsplit("\n", 1)[-1])
            if "findings" in payload:
                payload = payload["findings"]
            if "report" in payload:
                text = json.dumps(
                    {
                        "accepted_claim_ids": [
                            claim["id"] for claim in payload["report"]["claims"]
                        ],
                        "issues": [],
                    }
                )
            else:
                report_findings: list[FixtureClaim] = [
                    finding
                    for worker in payload["workers"]
                    for finding in worker.get("findings", [])
                ]
                if self.use_saved_evidence:
                    report_findings = [
                        {
                            "id": f"saved-{source['source_id']}-{excerpt['start_line'] + index}",
                            "text": line,
                            "citations": [
                                {
                                    "source_id": source["source_id"],
                                    "start_line": excerpt["start_line"] + index,
                                    "end_line": excerpt["start_line"] + index,
                                }
                            ],
                        }
                        for source in payload.get("saved_evidence", {}).get("sources", [])
                        for excerpt in source["excerpts"]
                        for index, line in enumerate(excerpt["lines"])
                    ]
                claims: list[FixtureClaim] = [
                    {
                        "id": finding["id"],
                        "text": finding["text"],
                        "citations": finding["citations"],
                    }
                    for finding in report_findings
                ]
                lines = [
                    claim["text"]
                    + " "
                    + "".join(
                        f"[[source:{ref['source_id']}:L{ref['start_line']}-L{ref['end_line']}]]"
                        for ref in claim["citations"]
                    )
                    for claim in claims
                ]
                text = json.dumps(
                    {
                        "markdown": "## Summary\nSee the cited findings.\n\n## Findings\n"
                        + "\n\n".join(lines)
                        + "\n\n## Conflicts\nA current and archived prices differ; "
                        "both versions are recorded above.\n\n## Limitations\n"
                        "Independent reliability evidence was unavailable.",
                        "claims": claims,
                        "covered_question_ids": ["pricing", "strategy"],
                        "stop_reason": "questions_addressed",
                    }
                )
                if self.incomplete_draft and not self.draft_interrupted:
                    self.draft_interrupted = True
                    text = '{"markdown":"unfinished'
                    incomplete = True
        elif "delegate_research" in tool_names:
            if exchanges == 0:
                call = ModelToolCall(
                    str(uuid4()),
                    "research_checkpoint",
                    {
                        "phase": "scoping",
                        "subquestions": [
                            {"id": "pricing", "question": "Compare pricing", "status": "open"},
                            {"id": "strategy", "question": "Compare strategy", "status": "open"},
                        ],
                    },
                )
            elif exchanges == 1:
                call = ModelToolCall(
                    str(uuid4()),
                    "delegate_research",
                    {
                        "round_index": 1,
                        "objectives": [
                            "Product A pricing and strategy",
                            "Product B pricing and strategy",
                        ],
                    },
                )
            elif exchanges == 2:
                call = ModelToolCall(
                    str(uuid4()),
                    "research_checkpoint",
                    {
                        "phase": "checking_conflict",
                        "conflicts": ["A current price differs from its archived price."],
                        "gaps": ["Independent reliability evidence unavailable."],
                        "subquestions": [
                            {"id": "pricing", "question": "Compare pricing", "status": "addressed"},
                            {
                                "id": "strategy",
                                "question": "Compare strategy",
                                "status": "addressed",
                            },
                        ],
                    },
                )
        else:
            objective = str(json.loads(prompt)["objective"])
            product = "a" if "Product A" in objective else "b"
            if exchanges == 0:
                self.workers_started.add(product)
                if len(self.workers_started) == 2:
                    self.both_workers.set()
                await asyncio.wait_for(self.both_workers.wait(), 5)
                call = ModelToolCall(
                    str(uuid4()), "web_search", {"query": objective, "max_results": 2}
                )
            elif exchanges == 1:
                urls = [f"https://example.com/{product}/current"]
                if product == "a":
                    urls.append("https://example.com/a/archive")
                if self.pages_per_worker is not None:
                    urls.extend(
                        f"https://example.com/{product}/extra-{index}"
                        for index in range(self.pages_per_worker - len(urls))
                    )
                call = ModelToolCall(str(uuid4()), "web_read", {"urls": urls})
            elif exchanges == 2:
                pages = [
                    page
                    for message in request.messages
                    if message.role == "tool"
                    for page in json.loads(message.content).get("pages", [])
                ]
                findings = []
                for index, page in enumerate(pages):
                    for line in page["untrusted_excerpt"]:
                        findings.append(
                            {
                                "id": f"{product}-{index}-{line['line']}",
                                "text": line["text"],
                                "question_ids": ["pricing" if line["line"] == 1 else "strategy"],
                                "citations": [
                                    {
                                        "source_id": page["source_id"],
                                        "start_line": line["line"],
                                        "end_line": line["line"],
                                    }
                                ],
                            }
                        )
                call = ModelToolCall(
                    str(uuid4()),
                    "research_checkpoint",
                    {
                        "findings": findings,
                        "examined_source_ids": [page["source_id"] for page in pages],
                        **(
                            {"stop_reason": "Collection is complete; use the saved findings."}
                            if self.checkpoint_stop_reason
                            else {}
                        ),
                    },
                )
        yield ModelStreamEvent("text_delta", text=text)
        if call is not None:
            yield ModelStreamEvent("tool_call", tool_call=call)
        yield ModelStreamEvent(
            "usage",
            input_tokens=200,
            output_tokens=4096 if incomplete else 100,
            cached_tokens=0,
            cache_creation_tokens=0,
        )
        yield ModelStreamEvent(
            "completed",
            finish_kind="incomplete" if incomplete else "tool" if call is not None else "final",
            finish_reason="max_output_tokens" if incomplete else None,
        )


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchExecutionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = database_tests.ResearchPostgresTests()
        await asyncio.to_thread(self.fixture.setUp)
        await asyncio.to_thread(
            self.fixture.sql,
            "DELETE FROM trellis.research_tasks WHERE run_id="
            + database_tests.literal(self.fixture.run_id)
            + ";",
            authenticated=False,
        )
        settings = Settings(
            _env_file=None,
            supabase_url="https://fixture.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        )
        owner = UUID(self.fixture.owner)
        self.tokens = VerifiedTokenSource(owner)
        self.tokens.record_verified("fixture-token", owner, datetime.now(UTC) + timedelta(hours=1))
        self.settings = settings
        self.installation_id = str(uuid4())
        self.transport = PostgresTransport(self.fixture)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        self.repository = SupabaseRepository(settings, owner, self.tokens, self.client)
        run = await self.repository.get_run(self.fixture.run_id)
        assert run is not None
        self.repository._record_lease(run, self.fixture.capability)
        self.service, self.provider, self.pages = self.make_service(self.repository)
        session = await self.repository.get_session(run.session_id)
        assert session is not None
        model = next(
            model for model in await self.repository.list_models() if model.id == run.model_id
        )
        request_id = str(uuid4())
        contract = await self.contracts.create(session, request_id, model)
        await self.repository.transition_run_record(
            run.id, RunStatus.FAILED, RunEventType.FAILED, {"code": "fixture_retired"}
        )
        created = await self.repository.create_run(
            session.id,
            str(uuid4()),
            request_id,
            "Compare A and B",
            model,
            recovery_contract=contract,
            run_mode="research",
        )
        self.fixture.run_id = created.id
        self.fixture.capability = self.repository._run_capability(created.id)
        self.execution_run = created

    def make_service(
        self, repository: SupabaseRepository
    ) -> tuple[RunService, ScriptedResearcher, FixturePages]:
        capabilities = RuntimeCapabilities(frozenset({"web_search", "web_read"}))
        pages = FixturePages()
        web = WebToolService(
            WebEvidenceService(repository, repository, owner_id=self.fixture.owner),
            retriever=pages,
            search=FixtureSearch(),
            search_key_hash="a" * 64,
        )
        tools = ToolRegistry(
            capabilities=capabilities,
            additional_tools=web.registrations(search_ready=True, read_ready=True),
        )
        provider = ScriptedResearcher()
        self.contracts = RecoveryContracts(
            self.installation_id,
            repository,
            tools,
            None,
            capabilities,
            frozenset({self.execution_run.adapter_kind})
            if hasattr(self, "execution_run")
            else frozenset({"openai", "anthropic"}),
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
            tool_registry=tools,
            lease_repository=repository,
            capabilities=capabilities,
            research_repository=repository,
            research_reports=ResearchReportService(repository, repository),
            recovery_repository=repository,
            recovery_compatibility=self.contracts.compatibility,
        )
        return service, provider, pages

    async def asyncTearDown(self) -> None:
        await self.service.close()
        await self.client.aclose()
        await asyncio.to_thread(self.fixture.tearDown)

    async def test_real_runtime_parallel_research_publishes_checked_citations_once(self) -> None:
        await self.service._execute_snapshot(self.execution_run)
        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.COMPLETED, saved.error_message)
        snapshot = await self.repository.read_research(self.execution_run.id)
        assert snapshot is not None
        self.assertIsNotNone(snapshot.report_model_call_id)
        self.assertEqual(len([task for task in snapshot.tasks if task.role == "worker"]), 2)
        self.assertEqual(len(self.pages.reads), 3)
        self.assertEqual(len(self.provider.workers_started), 2)
        messages = await self.repository.list_messages(self.execution_run.session_id)
        self.assertEqual(sum(message.role == "assistant" for message in messages), 1)
        report = next(message.content for message in messages if message.role == "assistant")
        self.assertIn("Current A price is $10.", report)
        self.assertIn("Archived A price is $8.", report)
        self.assertIn("B requires a sales quote.", report)
        self.assertIn("reliability evidence", report)
        self.assertNotIn("Internal research notes", report)
        citations = await self.repository.read_web_citations(
            self.execution_run.session_id, snapshot.report_model_call_id or ""
        )
        self.assertEqual(len(citations), 6)
        self.assertLessEqual(self.provider.calls, self.execution_run.max_model_calls)
        outputs = await asyncio.to_thread(
            self.fixture.sql,
            "SELECT count(*) FROM trellis.run_assistant_outputs WHERE run_id="
            + database_tests.literal(self.execution_run.id)
            + ";",
        )
        self.assertEqual(int(outputs.stdout.strip()), 1)

    async def test_incomplete_draft_retains_usage_and_repairs_before_publication(self) -> None:
        self.provider.incomplete_draft = True
        await self.service._execute_snapshot(self.execution_run)
        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.COMPLETED, saved.error_message)
        state = await self.repository.read_research(saved.id)
        assert state is not None
        calls = await self.repository.list_model_calls(saved.id)
        interrupted = [call for call in calls if call.error_code == "provider_response_incomplete"]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0].status.value, "completed")
        self.assertEqual(interrupted[0].accounted_total_tokens, 4296)
        self.assertGreater(interrupted[0].estimated_cost or 0, 0)
        self.assertEqual(self.provider.report_output_limits, [12288, 12288, 12288])
        self.assertEqual(
            [task.objective for task in state.tasks if task.role in {"writer", "reviewer"}],
            ["Research report draft", "Research report repair", "Research report recheck"],
        )
        messages = await self.repository.list_messages(saved.session_id)
        reports = [message.content for message in messages if message.role == "assistant"]
        self.assertEqual(len(reports), 1)
        self.assertIn("Current A price is $10.", reports[0])
        self.assertNotIn("unfinished", reports[0])
        citations = await self.repository.read_web_citations(
            saved.session_id, state.report_model_call_id or ""
        )
        self.assertEqual(len(citations), 6)

    async def test_worker_stop_checkpoint_still_publishes_checked_findings(self) -> None:
        self.provider.checkpoint_stop_reason = True
        await self.service._execute_snapshot(self.execution_run)
        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.COMPLETED, saved.error_message)
        state = await self.repository.read_research(saved.id)
        assert state is not None
        workers = [task for task in state.tasks if task.role == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(task.checkpoint.get("findings") for task in workers))
        self.assertTrue(all(task.checkpoint.get("stop_reason") for task in workers))
        self.assertEqual(len(self.pages.reads), 3)
        messages = await self.repository.list_messages(saved.session_id)
        reports = [message.content for message in messages if message.role == "assistant"]
        self.assertEqual(len(reports), 1)
        self.assertIn("Current A price is $10.", reports[0])
        self.assertIn("B requires a sales quote.", reports[0])
        citations = await self.repository.read_web_citations(
            saved.session_id, state.report_model_call_id or ""
        )
        self.assertEqual(len(citations), 6)

    async def test_collection_cutoff_reports_saved_pages_without_worker_findings(self) -> None:
        self.provider.use_saved_evidence = True
        prepare = RunContext.prepare_task

        async def stop_collection(runtime, *args, **kwargs):
            if (
                kwargs["tools"]
                and sum(message.role == "assistant" for message in kwargs["messages"]) >= 2
            ):
                raise ApplicationError(
                    "research_finalization_reserve", "Preparing the report from saved pages."
                )
            return await prepare(runtime, *args, **kwargs)

        with patch.object(RunContext, "prepare_task", stop_collection):
            await self.service._execute_snapshot(self.execution_run)

        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.COMPLETED, saved.error_message)
        state = await self.repository.read_research(self.execution_run.id)
        assert state is not None
        workers = [task for task in state.tasks if task.role == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(task.result is None for task in workers))
        self.assertTrue(all(not task.checkpoint.get("findings") for task in workers))
        self.assertTrue(
            all(
                task.checkpoint.get("stop_reason") == "research_finalization_reserve"
                for task in workers
            )
        )
        self.assertEqual(len(self.pages.reads), 3)
        messages = await self.repository.list_messages(self.execution_run.session_id)
        reports = [message.content for message in messages if message.role == "assistant"]
        self.assertEqual(len(reports), 1)
        self.assertIn("Current A price is $10.", reports[0])
        self.assertIn("Archived A price is $8.", reports[0])
        self.assertIn("B requires a sales quote.", reports[0])
        self.assertNotIn("Internal research notes", reports[0])
        citations = await self.repository.read_web_citations(
            self.execution_run.session_id, state.report_model_call_id or ""
        )
        self.assertEqual(len(citations), 6)
        self.assertLessEqual(self.provider.calls, self.execution_run.max_model_calls)

    async def test_recovery_after_ten_pages_keeps_original_budget_and_does_not_refetch(
        self,
    ) -> None:
        self.provider.pages_per_worker = 5
        self.transport.fail_delegate_results = True
        await self.service._execute_snapshot(self.execution_run)
        self.assertTrue(self.transport.failed)
        self.assertEqual(len(self.pages.reads), 10)
        saved = await self.repository.get_run(self.execution_run.id)
        assert saved is not None
        self.assertEqual(saved.status, RunStatus.RUNNING)
        self.assertFalse(self.repository.owns_run_lease(self.execution_run.id))
        self.assertTrue(
            all(
                call.status.value == "completed"
                for call in await self.repository.list_model_calls(self.execution_run.id)
            )
        )
        transport = PostgresTransport(self.fixture)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            repository = SupabaseRepository(
                self.settings, UUID(self.fixture.owner), self.tokens, client
            )
            service, provider, pages = self.make_service(repository)
            try:
                claimed, reason = await repository.claim_recovery(saved, self.installation_id)
                self.assertIsNone(reason)
                self.assertEqual(claimed.id, self.execution_run.id)
                self.assertEqual(claimed.deadline_at, self.execution_run.deadline_at)
                self.assertEqual(claimed.max_cost_usd, self.execution_run.max_cost_usd)
                self.assertEqual(claimed.max_total_tokens, self.execution_run.max_total_tokens)
                self.assertEqual(claimed.generation, self.execution_run.generation + 1)
                await service._execute_recovery(claimed)
                finished = await repository.get_run(self.execution_run.id)
                assert finished is not None
                self.assertEqual(finished.status, RunStatus.COMPLETED, finished.error_message)
                self.assertEqual(finished.recovery_count, 1)
                self.assertEqual(pages.reads, [])
                self.assertEqual(provider.workers_started, set())
                sources = await repository.list_web_sources(self.execution_run.session_id, 0, 20)
                self.assertEqual(len(sources.items), 10)
                messages = await repository.list_messages(self.execution_run.session_id)
                self.assertEqual(sum(message.role == "assistant" for message in messages), 1)
            finally:
                await service.close()
