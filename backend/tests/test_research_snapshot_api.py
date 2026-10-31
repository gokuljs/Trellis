import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import test_research_agent_loop as agent_tests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.auth import VerifiedUser
from app.api.routes.research import get_research, router
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.research import (
    RESEARCH_POLICY_V2,
    ResearchPhase,
    ResearchSnapshot,
    ResearchTask,
    ResearchTaskStatus,
)
from app.domain.runtime import RunStatus
from app.infrastructure.accounts import AccountContext

OWNER, SOURCE, TOOL, TASK = (str(uuid4()) for _ in range(4))


def fixtures():
    run = replace(
        agent_tests.make_run(),
        research_policy_version=RESEARCH_POLICY_V2,
        execution_budget_seconds=2700,
        budget_preset="longer",
    )
    citation = {"source_id": SOURCE, "start_line": 1201, "end_line": 1202}
    checkpoint: dict[str, object] = {
        "conversation_context": "Private earlier conversation must never be serialized",
        "brief": {
            "scope": "Compare methods and measured results",
            "comparison_dimensions": ["Methods", "Results"],
            "source_restrictions": ["Primary papers"],
            "expected_output": "A cited comparison",
        },
        "subquestions": [
            {
                "id": "q1",
                "question": "How do the methods and results differ?",
                "status": "addressed",
                "evidence_requirements": [
                    {"id": "methods", "description": "Compare methods", "depth": "full_text"},
                    {
                        "id": "results",
                        "description": "Compare evaluation results",
                        "depth": "full_text",
                    },
                ],
            }
        ],
        "findings": [
            {
                "id": "f1",
                "text": "A reported method",
                "question_ids": ["q1"],
                "requirement_ids": ["methods"],
                "citations": [citation],
                "evidence_depth": "full_text",
            }
        ],
        "gaps": [
            {"id": "access", "text": "Full text unavailable", "status": "resolved"},
            {"id": "results", "text": "Comparable results are missing", "status": "open"},
        ],
    }
    task = ResearchTask(
        TASK,
        run.id,
        "Compare evidence",
        "worker",
        1,
        ResearchTaskStatus.RUNNING,
        0,
        {"gaps": [{"id": "access", "text": "Full text unavailable", "status": "open"}]},
    )
    read: dict[str, object] = {
        "tool_call_id": TOOL,
        "task_id": TASK,
        "message_ordinal": 5,
        "source_id": SOURCE,
        "content_hash": "a" * 64,
        "read_kind": "source_lines",
        "presented_ranges": [{"start_line": 1200, "end_line": 1250}],
    }
    state = ResearchSnapshot(
        run.id,
        run.session_id,
        OWNER,
        ResearchPhase.RESEARCHING,
        "Compare papers",
        0,
        checkpoint,
        (task,),
        None,
        1200,
        policy_version=RESEARCH_POLICY_V2,
        execution_budget_seconds=2700,
        source_reads=(
            read,
            read,
            {
                **read,
                "tool_call_id": str(uuid4()),
                "message_ordinal": 7,
                "presented_ranges": [{"start_line": 1500, "end_line": 1550}],
            },
        ),
    )
    database = SimpleNamespace(read_research=AsyncMock(return_value=state))
    account = cast(
        AccountContext,
        SimpleNamespace(
            run_service=SimpleNamespace(get_run=AsyncMock(return_value=run)), database=database
        ),
    )
    return run, state, account


class ResearchSnapshotApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_worker_handoff_has_a_human_status_and_objective(self):
        run, state, account = fixtures()
        worker = replace(
            state.tasks[0],
            status=ResearchTaskStatus.FAILED,
            objective="Read the methods\nQuestions: q1\nPurpose: fill_gap",
            checkpoint={"stop_reason": "research_worker_burst_complete"},
        )
        state = replace(state, tasks=(worker,))
        account.database.read_research.return_value = state
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.tasks[0].status, "handed_off")
        self.assertEqual(result.tasks[0].objective, "Read the methods")

    async def test_failed_worker_keeps_checkpoint_evidence_and_gaps_visible(self):
        run, state, account = fixtures()
        finding = state.checkpoint["findings"][0]
        gap = {"id": "worker-gap", "text": "Comparable evaluation is missing", "status": "open"}
        worker = replace(
            state.tasks[0],
            status=ResearchTaskStatus.FAILED,
            checkpoint={"findings": [finding], "gaps": [gap]},
            result={"error_code": "research_checkpoint_required"},
        )
        state = replace(
            state, checkpoint={**state.checkpoint, "findings": [], "gaps": []}, tasks=(worker,)
        )
        account.database.read_research.return_value = state
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.coverage[0].requirements[0].status, "evidence_recorded")
        self.assertEqual(result.gaps, [gap["text"]])
        self.assertEqual(result.evidence_depth, ["full_text"])

    async def test_v2_snapshot_shows_recorded_coverage_and_actual_read_counts(self):
        run, state, account = fixtures()
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.execution_budget_seconds, 2700)
        self.assertEqual(result.policy_version, RESEARCH_POLICY_V2)
        self.assertEqual(result.sources_read, 1)
        self.assertEqual(result.source_windows_read, 2)
        self.assertEqual(result.evidence_depth, ["full_text"])
        self.assertEqual(result.gaps, ["Comparable results are missing"])
        self.assertEqual(result.coverage[0].status, "missing")
        self.assertEqual(result.subquestions[0].status, "open")
        self.assertEqual(
            [item.status for item in result.coverage[0].requirements],
            ["evidence_recorded", "missing"],
        )
        self.assertNotIn("Private earlier conversation", result.model_dump_json())
        assert result.brief is not None
        expected_brief = state.checkpoint["brief"]
        assert isinstance(expected_brief, dict)
        self.assertEqual(result.brief.scope, expected_brief["scope"])

    async def test_only_published_review_can_mark_requirement_reviewed(self):
        for published in (False, True):
            with self.subTest(published=published):
                run, state, account = fixtures()
                review = (
                    {"question_id": "q1", "requirement_ids": ["methods"], "claim_ids": ["claim1"]},
                )
                state = replace(
                    state,
                    reviewed_coverage=review,
                    phase=ResearchPhase.PARTIAL if published else ResearchPhase.RESEARCHING,
                    report_model_call_id=str(uuid4()) if published else None,
                )
                run = replace(run, status=RunStatus.COMPLETED if published else RunStatus.RUNNING)
                account.database.read_research.return_value = state
                account.run_service.get_run.return_value = run
                result = await get_research(UUID(run.session_id), UUID(run.id), account)
                self.assertEqual(
                    result.coverage[0].requirements[0].status,
                    "reviewed" if published else "evidence_recorded",
                )
                self.assertEqual(result.coverage[0].status, "missing")

    async def test_complete_question_review_requires_all_requirements(self):
        run, state, account = fixtures()
        state = replace(
            state,
            phase=ResearchPhase.COMPLETED,
            report_model_call_id=str(uuid4()),
            reviewed_coverage=(
                {
                    "question_id": "q1",
                    "requirement_ids": ["methods", "results"],
                    "claim_ids": ["claim1", "claim2"],
                },
            ),
        )
        account.database.read_research.return_value = state
        account.run_service.get_run.return_value = replace(run, status=RunStatus.COMPLETED)
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.coverage[0].status, "reviewed")
        self.assertEqual(result.subquestions[0].status, "addressed")

    async def test_legacy_snapshot_retains_old_allowance_without_review_badges(self):
        run, state, account = fixtures()
        run = replace(
            run, research_policy_version="bounded-research-v1", execution_budget_seconds=1200
        )
        state = replace(
            state,
            policy_version="bounded-research-v1",
            execution_budget_seconds=None,
            source_reads=(),
            reviewed_coverage=(),
            checkpoint={"gaps": ["Missing detail"]},
        )
        account.database.read_research.return_value = state
        account.run_service.get_run.return_value = run
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.execution_budget_seconds, 1200)
        self.assertEqual(result.coverage, [])
        self.assertEqual(result.evidence_depth, [])
        self.assertEqual(result.gaps, ["Missing detail"])
        self.assertIsNone(result.sources_read)
        self.assertIsNone(result.source_windows_read)

    async def test_foreign_session_is_rejected_before_reading_research_state(self):
        run, _, account = fixtures()
        with self.assertRaises(ApplicationError) as raised:
            await get_research(uuid4(), UUID(run.id), account)
        self.assertEqual(raised.exception.code, "run_not_found")
        account.database.read_research.assert_not_awaited()

    def test_snapshot_endpoint_requires_authenticated_account(self):
        run, _, account = fixtures()
        app = FastAPI()
        app.include_router(router)
        app.state.settings = Settings(environment="test", _env_file=None)

        async def verify(authorization):
            if authorization != "Bearer valid":
                raise HTTPException(status_code=401)
            return VerifiedUser(UUID(OWNER), None)

        app.state.auth_verifier = SimpleNamespace(verify_bearer=verify)
        registry = AsyncMock(return_value=account)
        app.state.account_registry = SimpleNamespace(get=registry)

        @app.exception_handler(ApplicationError)
        async def error_handler(request, error):
            return JSONResponse(status_code=404, content={"code": error.code})

        with TestClient(app) as client:
            url = f"/api/sessions/{run.session_id}/runs/{run.id}/research"
            self.assertEqual(client.get(url).status_code, 401)
            account.database.read_research.assert_not_awaited()
            response = client.get(url, headers={"Authorization": "Bearer valid"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["sources_read"], 1)
            self.assertNotIn("Private earlier conversation", json.dumps(response.json()))
        self.assertEqual(registry.await_count, 1)


if __name__ == "__main__":
    unittest.main()
