import unittest
from copy import deepcopy
from dataclasses import replace
from typing import cast
from uuid import uuid4

from app.application.errors import ApplicationError
from app.application.research import ResearchCoordinator
from app.application.research_ports import ResearchRepository
from app.domain.research import (
    LEGACY_RESEARCH_POLICY,
    RESEARCH_POLICY_V2,
    ResearchTask,
    ResearchTaskStatus,
)
from tests.test_research_agent_loop import make_run
from tests.test_research_followups import snapshot
from tests.test_research_policy import finding, question


class SourceCheckingRepository:
    def __init__(self, run, error=None):
        self.run = run
        self.error = error
        self.blocked_source = str(uuid4())
        self.task = ResearchTask(
            str(uuid4()),
            run.id,
            "Read official job listings",
            "worker",
            1,
            ResearchTaskStatus.RUNNING,
            0,
            {},
        )
        self.attempts = []
        self.persisted = []

    async def read_research(self, run_id):
        return snapshot(self.run, {"subquestions": [question()]}, (self.task,))

    async def checkpoint_research(
        self, run_id, generation, task_id, revision, operation_id, checkpoint
    ):
        self.attempts.append(deepcopy(checkpoint))
        if self.error is not None:
            raise self.error
        if any(
            reference["source_id"] == self.blocked_source
            for gap in checkpoint.get("gaps", [])
            for reference in gap.get("citations", [])
        ):
            raise ApplicationError("research_source_changed", "Private database diagnostic")
        self.task = replace(self.task, checkpoint=checkpoint, revision=self.task.revision + 1)
        self.persisted.append(deepcopy(checkpoint))
        return self.task


class ResearchCheckpointErrorTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, *, policy=RESEARCH_POLICY_V2, error=None):
        run = replace(make_run(), research_policy_version=policy)
        repository = SourceCheckingRepository(run, error)

        async def unused(task):
            raise AssertionError("Checkpointing cannot delegate")

        coordinator = ResearchCoordinator(cast(ResearchRepository, repository), run, unused)
        return repository, coordinator

    async def test_invalid_failed_source_citation_can_be_repaired_without_losing_valid_findings(
        self,
    ):
        repository, coordinator = self.harness()
        valid = finding()
        gap = {
            "id": "blocked-listing",
            "text": "The other employer listing was unavailable.",
            "question_ids": ["methods"],
            "requirement_ids": ["methods-text"],
            "status": "unavailable",
            "citations": [{"source_id": repository.blocked_source, "start_line": 1, "end_line": 1}],
        }
        arguments = {
            "findings": [valid],
            "gaps": [gap],
            "examined_source_ids": [repository.blocked_source],
        }
        response = await coordinator.control(
            repository.task, "research_checkpoint", arguments, str(uuid4())
        )
        self.assertEqual(response["status"], "invalid_request")
        self.assertIn("available", response["reason"])
        self.assertNotIn("Private database diagnostic", response["reason"])
        self.assertEqual(repository.task.checkpoint, {})
        self.assertEqual(repository.task.revision, 0)
        self.assertEqual(repository.persisted, [])
        corrected = {**arguments, "gaps": [{**gap, "citations": []}]}
        saved = await coordinator.control(
            repository.task, "research_checkpoint", corrected, str(uuid4())
        )
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(repository.task.revision, 1)
        self.assertEqual(len(repository.persisted), 1)
        self.assertEqual(repository.task.checkpoint["findings"], [valid])
        self.assertEqual(repository.task.checkpoint["gaps"][0]["citations"], [])
        self.assertEqual(
            repository.task.checkpoint["examined_source_ids"], [repository.blocked_source]
        )

    async def test_cancellation_unknown_usage_ownership_and_database_errors_still_propagate(self):
        for code in (
            "run_lease_lost",
            "research_scope_invalid",
            "research_revision_changed",
            "research_usage_unknown",
            "database_unavailable",
            "web_scope_invalid",
        ):
            with self.subTest(code=code):
                error = ApplicationError(code, "Preserve the original failure")
                repository, coordinator = self.harness(error=error)
                with self.assertRaises(ApplicationError) as raised:
                    await coordinator.control(
                        repository.task,
                        "research_checkpoint",
                        {"findings": [finding()]},
                        str(uuid4()),
                    )
                self.assertIs(raised.exception, error)
                self.assertEqual(repository.persisted, [])

    async def test_legacy_checkpoint_error_contract_is_unchanged(self):
        error = ApplicationError("research_source_changed", "Legacy source error")
        repository, coordinator = self.harness(policy=LEGACY_RESEARCH_POLICY, error=error)
        with self.assertRaises(ApplicationError) as raised:
            await coordinator.control(repository.task, "research_checkpoint", {}, str(uuid4()))
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
