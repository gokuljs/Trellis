import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from app.application.errors import ApplicationError
from app.application.research_task_context import project_research_task_context
from app.domain.research import ResearchTaskStatus
from app.domain.runtime import ModelCallStatus, ModelStreamEvent, ModelToolCall
from tests import test_research_handoff_runtime as handoff_tests


class DelayedResearcher(handoff_tests.CheckpointResearcher):
    async def stream(self, request, api_key, user_id):
        state = json.loads(request.messages[0].content)
        if state.get("worker_turns_remaining") == 6:
            self.requests.append(request)
            yield ModelStreamEvent(
                "tool_call",
                tool_call=ModelToolCall(
                    str(uuid4()), "research_checkpoint", {"phase": "researching"}
                ),
            )
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=30,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="tool")
            return
        async for event in super().stream(request, api_key, user_id):
            yield event


class RepetitiveResearcher(handoff_tests.CheckpointResearcher):
    async def stream(self, request, api_key, user_id):
        state = json.loads(request.messages[0].content)
        if state["checkpoint"].get("findings") and not state.get("checkpoint_required"):
            self.requests.append(request)
            yield ModelStreamEvent(
                "tool_call",
                tool_call=ModelToolCall(str(uuid4()), "research_checkpoint", {}),
            )
            yield ModelStreamEvent(
                "usage",
                input_tokens=16_000,
                output_tokens=30,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="tool")
            return
        discoveries = 0
        async for event in super().stream(request, api_key, user_id):
            if event.kind == "usage":
                event = replace(event, input_tokens=16_000)
            if event.tool_call is not None and event.tool_call.name == "web_read":
                discoveries += 1
                if discoveries > 2:
                    continue
                event = replace(
                    event,
                    tool_call=replace(
                        event.tool_call,
                        arguments={
                            "url": f"https://example.com/{state['objective']}/{discoveries}"
                        },
                    ),
                )
            yield event


class CorrectingHandoffResearcher(DelayedResearcher):
    def __init__(self, *, invalid_attempts=1):
        super().__init__()
        self.invalid_attempts = invalid_attempts

    async def stream(self, request, api_key, user_id):
        state = json.loads(request.messages[0].content)
        invalid = sum(
            message.role == "tool"
            and json.loads(message.content).get("status") == "invalid_request"
            for message in request.messages
        )
        if state.get("worker_turns_remaining") == 0 and invalid < self.invalid_attempts:
            self.requests.append(request)
            yield ModelStreamEvent(
                "tool_call",
                tool_call=ModelToolCall(str(uuid4()), "research_checkpoint", {}),
            )
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=30,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            yield ModelStreamEvent("completed", finish_kind="tool")
            return
        async for event in super().stream(request, api_key, user_id):
            yield event


class WorkerBurstTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_yields_saved_notes_after_eight_turns(self):
        run, records, provider, pages, service, execution, coordinator = (
            handoff_tests.HandoffRuntimeTests().harness()
        )
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), 8)
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_worker_burst_complete")
        self.assertEqual(len(records.task.checkpoint["findings"]), 6)
        self.assertEqual(len(pages.fetches), 10)
        self.assertEqual(records.holds, {})

    async def test_two_greedy_workers_leave_collection_capacity_for_a_third_assignment(self):
        run, records, _provider, pages, service, execution, coordinator = (
            handoff_tests.HandoffRuntimeTests().harness()
        )
        run = replace(
            run,
            budget_preset="longer",
            max_total_tokens=600_000,
            max_model_calls=120,
            max_tool_calls=384,
            max_cost_usd=15,
            execution_budget_seconds=2700,
            deadline_at=(
                datetime.fromisoformat(run.deadline_at) + timedelta(seconds=2100)
            ).isoformat(),
        )
        records.run = run
        coordinator.run = run
        reserve = records.reserve_research_call

        async def reserve_with_root_limits(*args, **kwargs):
            used_tokens, used_cost = service._budget_totals(run.model_id, records.calls)
            held = {
                identity: allowance
                for identity, allowance in records.holds.items()
                if not (kwargs.get("handoff") and identity == args[2])
            }
            if (
                used_tokens + args[5] + sum(value[0] for value in held.values())
                > run.max_total_tokens * 0.75
                or len(records.calls) + 1 + len(held) > run.max_model_calls - 4
                or (used_cost or 0) + args[6] + sum(value[1] for value in held.values())
                > run.max_cost_usd * 0.75
            ):
                raise ApplicationError("research_finalization_reserve", "Final review is reserved")
            return await reserve(*args, **kwargs)

        records.reserve_research_call = reserve_with_root_limits
        original = records.task
        completed = []
        for index in range(3):
            records.previous_tasks = tuple(completed)
            records.task = replace(
                original,
                id=str(uuid4()),
                objective=f"facet-{index}",
                round_index=1 if index < 2 else 2,
            )
            provider = RepetitiveResearcher()
            await service._run_research_task(
                run, replace(execution, provider=provider), coordinator, records.task
            )
            completed.append(records.task)
            self.assertEqual(len(provider.requests), 8)
            self.assertEqual(
                records.task.checkpoint["stop_reason"], "research_worker_burst_complete"
            )
            self.assertEqual(len(records.task.checkpoint["findings"]), 2)
            self.assertIs(records.task.status, ResearchTaskStatus.FAILED)
        used_tokens, used_cost = service._budget_totals(run.model_id, records.calls)
        self.assertEqual(len(records.calls), 24)
        self.assertEqual(len(pages.fetches), 6)
        self.assertLess(used_tokens, run.max_total_tokens * 0.75)
        self.assertLess(used_cost, run.max_cost_usd * 0.75)
        self.assertGreaterEqual(run.max_model_calls - len(records.calls), 4)
        self.assertEqual(records.holds, {})
        self.assertEqual(
            [
                json.loads(request.messages[0].content)["worker_turns_remaining"]
                for request in provider.requests
            ],
            list(range(8, 0, -1)),
        )
        self.assertTrue(all(call.status is ModelCallStatus.COMPLETED for call in records.calls))

    async def test_eighth_turn_reads_are_saved_by_frozen_ninth_handoff_after_recovery(self):
        run, records, _provider, pages, service, execution, coordinator = (
            handoff_tests.HandoffRuntimeTests().harness()
        )
        provider = DelayedResearcher()
        execution = replace(execution, provider=provider)
        original = records.record_tool_result

        async def lose_eighth_receipt(*args, **kwargs):
            result = await original(*args, **kwargs)
            if len(records.calls) == 8 and json.loads(result[0].content).get("substantive") is True:
                records.record_tool_result = original
                raise ApplicationError("database_unavailable", "Controlled eighth-turn failure")
            return result

        records.record_tool_result = lose_eighth_receipt
        with self.assertRaises(ApplicationError):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), 8)
        await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(len(provider.requests), 9)
        self.assertEqual(len(pages.fetches), 10)
        self.assertEqual(len(pages.body_reads), 6)
        self.assertEqual(len(records.task.checkpoint["findings"]), 6)
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_worker_burst_complete")
        metadata = records.calls[-1].request_snapshot["research"]
        self.assertIs(metadata["checkpoint_only"], True)
        self.assertIs(metadata["stop_after_checkpoint"], True)
        self.assertEqual(metadata["stop_reason"], "research_worker_burst_complete")
        self.assertEqual(
            {tool.name for tool in provider.requests[-1].tools}, {"research_checkpoint"}
        )
        projection = project_research_task_context(
            run.id, records.task.id, records.messages, records.tools, records.calls
        )
        self.assertEqual(projection.unprocessed_reads, ())
        self.assertEqual(records.holds, {})

    async def test_burst_handoff_keeps_one_durable_correction_after_recovery(self):
        for invalid_attempts in (1, 2):
            with self.subTest(invalid_attempts=invalid_attempts):
                run, records, _provider, _pages, service, execution, coordinator = (
                    handoff_tests.HandoffRuntimeTests().harness()
                )
                provider = CorrectingHandoffResearcher(invalid_attempts=invalid_attempts)
                execution = replace(execution, provider=provider)
                original = records.record_tool_result

                async def lose_invalid_receipt(
                    *args, _records=records, _original=original, **kwargs
                ):
                    result = await _original(*args, **kwargs)
                    if (
                        len(_records.calls) == 9
                        and json.loads(result[0].content).get("status") == "invalid_request"
                    ):
                        _records.record_tool_result = _original
                        raise ApplicationError(
                            "database_unavailable", "Lost invalid handoff receipt"
                        )
                    return result

                records.record_tool_result = lose_invalid_receipt
                with self.assertRaises(ApplicationError):
                    await service._run_research_task(run, execution, coordinator, records.task)
                recovered = CorrectingHandoffResearcher(invalid_attempts=invalid_attempts)
                await service._run_research_task(
                    run, replace(execution, provider=recovered), coordinator, records.task
                )
                self.assertEqual(len(records.calls), 10)
                self.assertEqual(len(recovered.requests), 1)
                self.assertEqual(
                    records.task.checkpoint["stop_reason"],
                    "research_worker_burst_complete"
                    if invalid_attempts == 1
                    else "research_checkpoint_invalid",
                )
                projection = project_research_task_context(
                    run.id, records.task.id, records.messages, records.tools, records.calls
                )
                self.assertEqual(
                    len(projection.unprocessed_reads), 0 if invalid_attempts == 1 else 2
                )
                self.assertEqual(records.holds, {})
                self.assertFalse(records.unknown)


if __name__ == "__main__":
    unittest.main()
