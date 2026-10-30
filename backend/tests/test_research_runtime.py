import asyncio
import unittest
from typing import cast
from uuid import uuid4

from app.application.research import (
    ResearchCoordinator,
    ResearchGeneration,
    control_specs,
    merge_checkpoint,
)
from app.application.research_ports import ResearchRepository
from app.domain.research import ResearchTask, ResearchTaskInput, ResearchTaskStatus
from app.domain.runtime import RunSnapshot


class CheckpointTests(unittest.TestCase):
    def test_discoveries_survive_later_checkpoints(self) -> None:
        first = merge_checkpoint({}, {"findings": [{"id": "a", "text": "First finding"}]})
        second = merge_checkpoint(first, {"findings": [{"id": "b", "text": "Second finding"}]})
        self.assertEqual(
            [finding["id"] for finding in cast(list[dict[str, object]], second["findings"])],
            ["a", "b"],
        )
        self.assertEqual(
            merge_checkpoint(second, {"findings": [{"id": "b", "text": "Second finding"}]}), second
        )

    def test_reversed_evidence_ranges_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            merge_checkpoint(
                {},
                {
                    "findings": [
                        {
                            "id": "a",
                            "text": "Claim",
                            "citations": [
                                {
                                    "source_id": str(uuid4()),
                                    "start_line": 8,
                                    "end_line": 2,
                                }
                            ],
                        }
                    ]
                },
            )

    def test_workers_have_no_delegation_or_clarification_tool(self) -> None:
        self.assertEqual([spec.name for spec in control_specs("worker")], ["research_checkpoint"])


class _Tasks:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.tasks: tuple[ResearchTask, ...] = ()

    async def read_research(self, run_id: str) -> object:
        class Snapshot:
            tasks = self.tasks

        return Snapshot()

    async def create_research_tasks(
        self, run_id: str, generation: int, operation_id: str, tasks: tuple[ResearchTaskInput, ...]
    ) -> tuple[ResearchTask, ...]:
        self.tasks = tuple(
            ResearchTask(
                task.id,
                run_id,
                task.objective,
                task.role,
                task.round_index,
                ResearchTaskStatus.QUEUED,
                0,
                {},
            )
            for task in tasks
        )
        return self.tasks


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_workers_run_concurrently_and_are_joined(self) -> None:
        run_id = str(uuid4())
        repository = _Tasks(run_id)
        started: list[str] = []
        both_started = asyncio.Event()

        async def run_task(task: ResearchTask) -> ResearchGeneration:
            started.append(task.id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            return ResearchGeneration(str(uuid4()), "saved findings")

        class Run:
            id = run_id
            generation = 1
            budget_preset = "conservative"

        coordinator = ResearchCoordinator(
            cast(ResearchRepository, repository), cast(RunSnapshot, Run()), run_task
        )
        lead = ResearchTask(
            str(uuid4()), run_id, "Compare A and B", "lead", 0, ResearchTaskStatus.RUNNING, 0, {}
        )
        result = await coordinator.control(
            lead,
            "delegate_research",
            {
                "round_index": 1,
                "objectives": ["Research A", "Research B"],
            },
            str(uuid4()),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(started), 2)

    async def test_worker_cannot_delegate_even_with_forged_arguments(self) -> None:
        class Run:
            id = str(uuid4())
            generation = 1
            budget_preset = "conservative"

        async def unexpected_task(task: ResearchTask) -> ResearchGeneration:
            raise AssertionError("Workers cannot delegate")

        coordinator = ResearchCoordinator(
            cast(ResearchRepository, object()), cast(RunSnapshot, Run()), unexpected_task
        )
        worker = ResearchTask(
            str(uuid4()), Run.id, "Research A", "worker", 1, ResearchTaskStatus.RUNNING, 0, {}
        )
        result = await coordinator.control(
            worker,
            "delegate_research",
            {
                "round_index": 1,
                "objectives": ["Unbounded child"],
            },
            str(uuid4()),
        )
        self.assertEqual(result["status"], "invalid_request")

    async def test_cancel_joins_every_parallel_worker(self) -> None:
        run_id = str(uuid4())
        repository = _Tasks(run_id)
        entered = asyncio.Event()
        started: list[str] = []
        joined: list[str] = []

        async def run_task(task: ResearchTask) -> ResearchGeneration:
            started.append(task.id)
            if len(started) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.append(task.id)
            return ResearchGeneration(str(uuid4()), "unused")

        class Run:
            id = run_id
            generation = 1
            budget_preset = "conservative"

        coordinator = ResearchCoordinator(
            cast(ResearchRepository, repository), cast(RunSnapshot, Run()), run_task
        )
        lead = ResearchTask(
            str(uuid4()), run_id, "Compare", "lead", 0, ResearchTaskStatus.RUNNING, 0, {}
        )
        active = asyncio.create_task(
            coordinator.control(
                lead,
                "delegate_research",
                {
                    "round_index": 1,
                    "objectives": ["A", "B"],
                },
                str(uuid4()),
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertCountEqual(joined, started)


if __name__ == "__main__":
    unittest.main()
