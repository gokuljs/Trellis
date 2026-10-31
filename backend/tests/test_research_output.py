import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid5

import test_research_agent_loop as agent_loop

from app.application.errors import ProviderError
from app.application.model_catalog import context_budget, profile_for_model
from app.application.ports import SessionRepository, StreamingProviderAdapter
from app.application.research import ResearchGenerate, ResearchGeneration, ResearchReportFinalizer
from app.application.research_ports import ResearchRepository
from app.domain.research import ResearchSnapshot, ResearchTaskStatus
from app.domain.runtime import ModelCallStatus, ModelRequest, ModelStreamEvent, RunSnapshot


class FinalResponse:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, api_key: str, user_id: str):
        self.requests.append(request)
        yield ModelStreamEvent("text_delta", text="{}")
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=20, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent("completed", finish_kind="final")


class ResearchOutputTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, *, output: int | None = None, **run_values: object):
        run, _records, _provider, _pages, _service, execution, _coordinator = (
            agent_loop.AgentLoopTests().harness()
        )
        run = replace(run, **run_values)
        options = {} if output is None else {"max_output_tokens": output}
        return await execution.runtime.prepare_task(
            run,
            cast(StreamingProviderAdapter, object()),
            "",
            "user",
            task_id="task",
            prompt="Compare the saved evidence.",
            instructions="Return the requested JSON.",
            messages=[],
            tool_calls=[],
            tools=(),
            **options,
        )

    async def test_task_default_and_explicit_report_output_reserves(self) -> None:
        ordinary = await self.prepare()
        report = await self.prepare(output=12_288)
        self.assertEqual(ordinary.request.max_output_tokens, 4096)
        self.assertEqual(report.request.max_output_tokens, 12_288)
        self.assertEqual(report.output_reserve_tokens, 12_288)
        self.assertTrue(report.fits)

    async def test_report_reserve_respects_model_tokens_and_cost(self) -> None:
        profile = profile_for_model(agent_loop.make_run().model_id)
        assert profile is not None
        limited = replace(profile, max_output_tokens=8192)
        with patch("app.application.run_context.profile_for_model", return_value=limited):
            model_limited = await self.prepare(output=12_288)
        self.assertEqual(model_limited.request.max_output_tokens, 8192)

        token_limited = await self.prepare(output=12_288, max_total_tokens=5000)
        self.assertEqual(token_limited.input_tokens + token_limited.output_reserve_tokens, 5000)
        cost_limited = await self.prepare(output=12_288, max_cost_usd=0.05)
        self.assertLess(cost_limited.output_reserve_tokens, 12_288)
        assert cost_limited.estimated_call_cost_usd is not None
        self.assertLessEqual(cost_limited.estimated_call_cost_usd, 0.05)

    async def test_larger_output_reserves_full_context_headroom(self) -> None:
        profile = profile_for_model(agent_loop.make_run().model_id)
        assert profile is not None
        profile = replace(
            profile,
            context_window_tokens=32_000,
            max_output_tokens=16_000,
            max_input_tokens=30_000,
        )
        budget = context_budget(profile, output_reserve=12_288)
        assert budget is not None
        self.assertEqual(budget.output_reserve_tokens, 12_288)
        self.assertEqual(budget.usable_input_tokens, 32_000 - 12_288 - 2048)
        with patch("app.application.run_context.profile_for_model", return_value=profile):
            plan = await self.prepare(output=12_288)
        self.assertEqual(plan.usable_input_tokens, budget.usable_input_tokens)

    async def test_only_finalization_gets_three_times_the_worker_output_allowance(self) -> None:
        for finalization, expected in ((False, 4096), (True, 12_288)):
            with self.subTest(finalization=finalization):
                run, _records, _provider, _pages, service, execution, coordinator = (
                    agent_loop.AgentLoopTests().harness()
                )
                provider = FinalResponse()
                execution = replace(execution, provider=cast(StreamingProviderAdapter, provider))
                result = await service._run_research_task(
                    run, execution, coordinator, _records.task, finalization=finalization
                )
                self.assertIsNotNone(result)
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(provider.requests[0].max_output_tokens, expected)

    async def run_report_generation(
        self,
        *,
        cached: bool = False,
        call_values: Mapping[str, object] | None = None,
        error_code: str = "provider_response_incomplete",
    ):
        run, records, _provider, _pages, service, execution, _coordinator = (
            agent_loop.AgentLoopTests().harness()
        )
        task_id = str(uuid5(UUID(run.id), "report:draft"))
        task = replace(records.task, id=task_id, role="writer", round_index=0)
        _reservation, call = await records.reserve_research_call(
            run.id, run.generation, task.id, "saved", {}, 0, 0
        )
        call = replace(
            call,
            status=ModelCallStatus.COMPLETED,
            finish_reason="max_output_tokens",
            response_snapshot={
                "content": '{"markdown":"unfinished',
                "finish_kind": "incomplete",
                "finish_reason": "max_output_tokens",
            },
            input_tokens=100,
            output_tokens=4096,
            cached_tokens=0,
            cache_creation_tokens=0,
            accounted_total_tokens=4196,
            estimated_cost=0.1,
            error_code="provider_response_incomplete",
        )
        if call_values:
            call = replace(call, **call_values)
        records.calls.clear()
        repository = AsyncMock()
        repository.create_research_tasks.return_value = (
            replace(task, status=ResearchTaskStatus.FAILED) if cached else task,
        )
        repository.read_research_task_calls.return_value = [call]
        service._research_repository = cast(ResearchRepository, repository)
        sessions = AsyncMock()
        sessions.get_message.return_value = Mock(content="Compare employers")
        service._sessions = cast(SessionRepository, sessions)
        snapshot = await records.read_research(run.id)
        coordinator = Mock(waiting=False)
        coordinator.lead = AsyncMock(
            return_value=replace(task, role="lead", status=ResearchTaskStatus.COMPLETED)
        )
        coordinator.snapshot = AsyncMock(return_value=snapshot)
        results: list[ResearchGeneration] = []

        async def finalize(
            run: RunSnapshot, snapshot: ResearchSnapshot, generate: ResearchGenerate
        ) -> None:
            results.append(await generate("Write the report", "draft"))
            results.append(await generate("Write the report", "draft"))

        reports = AsyncMock()
        reports.finalize.side_effect = finalize
        service._research_reports = cast(ResearchReportFinalizer, reports)

        async def fail(*args: object, **kwargs: object):
            repository.create_research_tasks.return_value = (
                replace(task, status=ResearchTaskStatus.FAILED),
            )
            raise ProviderError(error_code, "The provider did not complete the response.")

        with (
            patch("app.application.runs.ResearchCoordinator", return_value=coordinator),
            patch.object(service, "_run_research_task", side_effect=fail) as execute,
        ):
            await service._execute_research(run, execution)
        self.assertEqual(execute.await_count, 0 if cached else 1)
        repository.reserve_research_call.assert_not_awaited()
        repository.commit_research_generation.assert_not_awaited()
        self.assertEqual(
            repository.create_research_tasks.return_value[0].status, ResearchTaskStatus.FAILED
        )
        self.assertEqual(call.accounted_total_tokens, 4196)
        self.assertEqual(call.estimated_cost, 0.1)
        assert call.response_snapshot is not None
        self.assertEqual(
            results,
            [ResearchGeneration(call.id, str(call.response_snapshot["content"]), incomplete=True)]
            * 2,
        )

    async def test_accounted_incomplete_report_reaches_validation_and_replays_without_work(
        self,
    ) -> None:
        await self.run_report_generation()

    async def test_saved_failed_incomplete_report_replays_without_model_work(self) -> None:
        await self.run_report_generation(cached=True)

    async def test_known_provider_output_limits_reach_validation(self) -> None:
        for reason in ("max_output_tokens", "max_tokens"):
            with self.subTest(reason=reason):
                await self.run_report_generation(
                    call_values={
                        "finish_reason": reason,
                        "response_snapshot": {
                            "content": '{"markdown":"unfinished',
                            "finish_kind": "incomplete",
                            "finish_reason": reason,
                        },
                    }
                )
        await self.run_report_generation(
            call_values={
                "response_snapshot": {
                    "content": '{"markdown":"unfinished',
                    "finish_kind": "incomplete",
                }
            }
        )

    async def test_other_missing_or_inconsistent_finish_reasons_are_not_repaired(self) -> None:
        for reason in ("content_filter", "incomplete", "unknown", None):
            with self.subTest(reason=reason):
                with self.assertRaises(ProviderError) as fault:
                    await self.run_report_generation(
                        call_values={
                            "finish_reason": reason,
                            "response_snapshot": {
                                "content": "{}",
                                "finish_kind": "incomplete",
                                "finish_reason": reason,
                            },
                        }
                    )
                self.assertEqual(fault.exception.code, "provider_response_incomplete")
        for snapshot_reason in ("max_tokens", "content_filter", None):
            with self.subTest(snapshot_reason=snapshot_reason):
                with self.assertRaises(ProviderError) as fault:
                    await self.run_report_generation(
                        call_values={
                            "response_snapshot": {
                                "content": "{}",
                                "finish_kind": "incomplete",
                                "finish_reason": snapshot_reason,
                            }
                        }
                    )
                self.assertEqual(fault.exception.code, "provider_response_incomplete")

    async def test_unaccounted_or_other_provider_failures_are_not_converted(self) -> None:
        for values in (
            {"status": ModelCallStatus.STREAMING},
            {"accounted_total_tokens": None},
            {"estimated_cost": None},
            {"error_code": "model_outcome_unknown"},
            {"response_snapshot": {"content": "{}", "finish_kind": "final"}},
        ):
            with self.subTest(call_values=values):
                with self.assertRaises(ProviderError) as fault:
                    await self.run_report_generation(call_values=values)
                self.assertEqual(fault.exception.code, "provider_response_incomplete")
        with self.assertRaises(ProviderError) as fault:
            await self.run_report_generation(error_code="provider_connection")
        self.assertEqual(fault.exception.code, "provider_connection")
