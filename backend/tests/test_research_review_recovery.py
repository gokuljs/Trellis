import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_research_agent_loop as agent_tests

from app.application.errors import ApplicationError
from app.application.ports import SessionRepository
from app.application.research import ResearchGeneration, ResearchReportFinalizer
from app.application.research_ports import ResearchRepository
from app.domain.research import ResearchTaskStatus
from app.domain.runtime import ModelCallStatus


class ReviewRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def harness(
        self,
        status,
        *,
        policy="bounded-research-v2",
        prompt="same report",
        saved_prompt="same report",
    ):
        run, records, _, _, service, execution, coordinator = agent_tests.AgentLoopTests().harness()
        run = replace(run, research_policy_version=policy)
        request = {
            "provider_id": run.provider_id,
            "model_id": run.model_id,
            "adapter_kind": run.adapter_kind,
            "upstream_model_id": run.upstream_model_id,
            "max_output_tokens": 4096,
            "messages": [{"role": "user", "content": saved_prompt}],
        }
        _, call = await records.reserve_research_call(
            run.id, run.generation, records.task.id, str(uuid4()), request, 100, 0.01
        )
        call = replace(call, status=status, response_snapshot={"content": "saved review"})
        task = replace(
            records.task,
            role="reviewer",
            status=ResearchTaskStatus.COMPLETED
            if status is ModelCallStatus.COMPLETED
            else ResearchTaskStatus.RUNNING,
        )
        repository = SimpleNamespace(
            create_research_tasks=AsyncMock(return_value=(task,)),
            read_research_task_calls=AsyncMock(return_value=[call]),
        )
        coordinator.lead = AsyncMock(
            return_value=replace(task, status=ResearchTaskStatus.COMPLETED)
        )
        coordinator.snapshot = AsyncMock(return_value=await records.read_research(run.id))
        service._research_repository = cast(ResearchRepository, repository)
        service._sessions = cast(
            SessionRepository,
            SimpleNamespace(
                get_message=AsyncMock(return_value=SimpleNamespace(content="question"))
            ),
        )
        service._budget_totals = lambda *_: (0, 0.0)
        service._append_and_publish = AsyncMock()
        service._run_research_task = AsyncMock(
            return_value=ResearchGeneration(call.id, "fresh review")
        )
        result = []

        async def finalize(_run, _state, generate):
            result.append(await generate(prompt, "recheck"))

        service._research_reports = cast(
            ResearchReportFinalizer, SimpleNamespace(finalize=finalize)
        )
        with patch("app.application.runs.ResearchCoordinator", return_value=coordinator):
            await service._execute_research(run, execution)
        return result, service._run_research_task

    async def test_changed_review_input_cannot_reuse_completed_or_pending_paid_request(self):
        for status in (ModelCallStatus.COMPLETED, ModelCallStatus.PENDING):
            with self.subTest(status=status):
                with self.assertRaises(ApplicationError) as error:
                    await self.harness(status, prompt="different report and source hashes")
                self.assertEqual(error.exception.code, "inconsistent_execution_records")

    async def test_identical_completed_review_is_reused_without_model_work(self):
        result, execute = await self.harness(ModelCallStatus.COMPLETED)
        self.assertEqual(result[0].content, "saved review")
        execute.assert_not_awaited()

    async def test_identical_pending_review_keeps_its_original_dispatch(self):
        result, execute = await self.harness(ModelCallStatus.PENDING)
        self.assertEqual(result[0].content, "fresh review")
        execute.assert_awaited_once()

    async def test_review_instruction_changes_preserve_frozen_report_and_evidence_recovery(self):
        payload = json.dumps(
            {"report": {"markdown": "exact report"}, "evidence": [{"content_hash": "saved hash"}]}
        )
        for status in (ModelCallStatus.PENDING, ModelCallStatus.COMPLETED):
            with self.subTest(status=status):
                result, _ = await self.harness(
                    status,
                    saved_prompt="Original review guidance.\n" + payload,
                    prompt="Clarified review guidance.\n" + payload,
                )
                self.assertTrue(result)

    async def test_changed_evidence_hash_or_report_cannot_reuse_review(self):
        payload = {"report": {"markdown": "exact report"}, "evidence": [{"content_hash": "hash"}]}
        for key, changed in (
            ("report", {"markdown": "changed report"}),
            ("evidence", [{"content_hash": "different hash"}]),
        ):
            with self.subTest(key=key), self.assertRaises(ApplicationError):
                await self.harness(
                    ModelCallStatus.COMPLETED,
                    saved_prompt="Guidance.\n" + json.dumps(payload),
                    prompt="Guidance.\n" + json.dumps({**payload, key: changed}),
                )

    async def test_legacy_completed_review_recovery_contract_is_unchanged(self):
        result, execute = await self.harness(
            ModelCallStatus.COMPLETED, policy="bounded-research-v1", prompt="legacy replay"
        )
        self.assertEqual(result[0].content, "saved review")
        execute.assert_not_awaited()
