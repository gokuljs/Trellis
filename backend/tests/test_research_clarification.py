import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import test_research_agent_loop as agent_tests

from app.api.routes.research import get_research
from app.api.routes.sessions import serialize_run_summary
from app.application.errors import ApplicationError
from app.application.ports import RunRepository, SessionRepository, SettingsRepository
from app.application.research import ResearchReportFinalizer
from app.application.research_ports import ResearchRepository
from app.application.run_capacity import RunCapacity
from app.domain.capabilities import RuntimeCapabilities
from app.domain.research import ResearchPhase, ResearchSnapshot, ResearchTask, ResearchTaskStatus
from app.domain.runtime import RunStatus, RunSummary
from app.infrastructure.accounts import AccountContext


class ClarificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_run_does_not_show_active_workers_after_reload(self) -> None:
        run = replace(agent_tests.make_run(), status=RunStatus.CANCELLED)
        task = ResearchTask(
            str(uuid4()), run.id, "Compare pricing", "worker", 1, ResearchTaskStatus.RUNNING, 0, {}
        )
        state = ResearchSnapshot(
            run.id,
            run.session_id,
            str(uuid4()),
            ResearchPhase.STOPPED,
            "Compare products",
            0,
            {},
            (task,),
            None,
            0,
        )
        account = cast(
            AccountContext,
            SimpleNamespace(
                run_service=SimpleNamespace(get_run=AsyncMock(return_value=run)),
                database=SimpleNamespace(read_research=AsyncMock(return_value=state)),
            ),
        )
        result = await get_research(UUID(run.session_id), UUID(run.id), account)
        self.assertEqual(result.tasks[0].status, "cancelled")

    async def test_research_admission_requires_actual_web_tools(self) -> None:
        run, _, _, _, service, execution, _ = agent_tests.AgentLoopTests().harness()
        service._sessions = cast(
            SessionRepository,
            SimpleNamespace(
                get_session=AsyncMock(
                    return_value=SimpleNamespace(id=run.session_id, model_id=run.model_id)
                )
            ),
        )
        service._runs = cast(
            RunRepository, SimpleNamespace(match_run_request=AsyncMock(return_value=None))
        )
        service._settings = cast(
            SettingsRepository,
            SimpleNamespace(list_models=AsyncMock(return_value=[execution.model])),
        )
        service._research_reports = cast(ResearchReportFinalizer, object())
        service._research_repository = cast(
            ResearchRepository, SimpleNamespace(research_ready=AsyncMock(return_value=True))
        )
        service._capabilities = RuntimeCapabilities(frozenset({"web_search", "web_read"}))
        with self.assertRaises(ApplicationError) as raised:
            await service._admit_run(
                run.session_id,
                str(uuid4()),
                "Compare competitors",
                model_id=run.model_id,
                budget_preset="conservative",
                run_mode="research",
            )
        self.assertEqual(raised.exception.code, "research_unavailable")

    async def test_replayed_answer_keeps_running_execution_capacity(self) -> None:
        run, _, _, _, service, _, _ = agent_tests.AgentLoopTests().harness()
        capacity = RunCapacity(max_active_runs=1, max_active_runs_per_user=1)
        reservation = capacity.reserve(service._owner_id, run.session_id, run.client_request_id)
        capacity.bind(reservation, run.id)
        service._capacity = capacity

        async def answer(*args: object):
            return run, ()

        service._research_repository = cast(
            ResearchRepository,
            SimpleNamespace(
                answer_research_clarification=answer,
            ),
        )
        result = await service.clarify_research(run.id, str(uuid4()), str(uuid4()), "India")
        self.assertEqual(result.id, run.id)
        with self.assertRaises(ApplicationError) as raised:
            capacity.reserve(service._owner_id, str(uuid4()), str(uuid4()))
        self.assertEqual(raised.exception.code, "run_capacity")

    def test_history_summary_preserves_research_mode(self) -> None:
        run = agent_tests.make_run()
        response = serialize_run_summary(RunSummary(run, 0, 0, 0, True, 0, True))
        self.assertEqual(response.run_mode, "research")


if __name__ == "__main__":
    unittest.main()
