import unittest
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import test_research_agent_loop as agent_tests

from app.api.routes.sessions import serialize_run_summary
from app.application.errors import ApplicationError
from app.application.research_ports import ResearchRepository
from app.application.run_capacity import RunCapacity
from app.domain.runtime import RunSummary


class ClarificationTests(unittest.IsolatedAsyncioTestCase):
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
