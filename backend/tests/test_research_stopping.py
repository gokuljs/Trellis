import json
import unittest
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.application.errors import ApplicationError
from app.application.ports import StreamingProviderAdapter
from app.domain.research import ResearchTaskStatus
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ToolCallRecord,
    ToolCallStatus,
)
from tests import test_research_agent_loop as loop_fixtures


class CheckpointResearcher:
    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, api_key: str, user_id: str):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelStreamEvent(
                "tool_call",
                tool_call=ModelToolCall(
                    "checkpoint",
                    "research_checkpoint",
                    {
                        "findings": [{"id": "saved", "text": "Saved finding"}],
                        "stop_reason": self.reason,
                    },
                ),
            )
            yield ModelStreamEvent(
                "tool_call",
                tool_call=ModelToolCall(
                    "remaining-read", "web_read", {"url": "https://example.com/not-needed"}
                ),
            )
        else:
            yield ModelStreamEvent("text_delta", text="Saved worker summary")
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=20, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent(
            "completed", finish_kind="tool" if len(self.requests) == 1 else "final"
        )


class ResearchStoppingTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, reason: str):
        run, records, _, pages, service, execution, coordinator = (
            loop_fixtures.AgentLoopTests().harness()
        )
        provider = CheckpointResearcher(reason)
        execution = replace(execution, provider=cast(StreamingProviderAdapter, provider))
        return run, records, provider, pages, service, execution, coordinator

    async def test_stop_checkpoint_preserves_findings_without_more_model_or_web_work(self) -> None:
        reason = "web collection budget exhausted after saving findings"
        run, records, provider, pages, service, execution, coordinator = self.harness(reason)
        result = await service._run_research_task(run, execution, coordinator, records.task)
        self.assertIsNone(result)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(pages.urls, [])
        self.assertEqual(records.task.status, ResearchTaskStatus.FAILED)
        self.assertEqual(records.task.checkpoint["findings"][0]["text"], "Saved finding")
        self.assertEqual(
            [tool.status for tool in records.tools],
            [ToolCallStatus.COMPLETED, ToolCallStatus.CANCELLED],
        )
        cancellation = json.loads(records.messages[-1].content)
        self.assertEqual(cancellation["reason"], reason)
        self.assertFalse(records.unknown)

    async def test_empty_or_invalid_checkpoint_stop_reason_does_not_stop_work(self) -> None:
        for reason in ("", "x" * 101):
            with self.subTest(reason=reason):
                run, records, provider, pages, service, execution, coordinator = self.harness(
                    reason
                )
                result = await service._run_research_task(run, execution, coordinator, records.task)
                self.assertIsNotNone(result)
                self.assertEqual(len(provider.requests), 2)
                self.assertEqual(pages.urls, ["https://example.com/not-needed"])
                self.assertEqual(records.task.status, ResearchTaskStatus.COMPLETED)
                self.assertTrue(
                    all(tool.status is ToolCallStatus.COMPLETED for tool in records.tools)
                )

    async def test_terminal_task_does_not_resume_pending_tools_or_reserve_model_work(self) -> None:
        run, records, provider, pages, service, execution, coordinator = self.harness("done")
        records.task = replace(
            records.task,
            status=ResearchTaskStatus.FAILED,
            checkpoint={
                "stop_reason": "done",
                "findings": [{"id": "saved", "text": "Saved finding"}],
            },
        )
        records.tools.append(
            ToolCallRecord(
                str(uuid4()),
                run.id,
                str(uuid4()),
                0,
                "pending-read",
                "web_read",
                {"url": "https://example.com/not-needed"},
                ToolCallStatus.PENDING,
                None,
                None,
                run.created_at,
                None,
                task_id=records.task.id,
            )
        )
        result = await service._run_research_task(run, execution, coordinator, records.task)
        self.assertIsNone(result)
        self.assertEqual(provider.requests, [])
        self.assertEqual(records.calls, [])
        self.assertEqual(pages.urls, [])
        self.assertEqual(records.tools[0].status, ToolCallStatus.CANCELLED)
        self.assertEqual(json.loads(records.messages[-1].content)["reason"], "done")
        self.assertEqual(records.task.checkpoint["findings"][0]["text"], "Saved finding")

    async def test_unrelated_scope_errors_still_propagate(self) -> None:
        run, records, provider, _, service, execution, coordinator = self.harness("done")
        with (
            patch.object(
                coordinator,
                "control",
                AsyncMock(
                    side_effect=ApplicationError("research_scope_invalid", "Stale generation")
                ),
            ),
            self.assertRaises(ApplicationError) as error,
        ):
            await service._run_research_task(run, execution, coordinator, records.task)
        self.assertEqual(error.exception.code, "research_scope_invalid")
        self.assertEqual(len(provider.requests), 1)


if __name__ == "__main__":
    unittest.main()
