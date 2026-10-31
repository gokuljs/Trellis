import json
import unittest
from dataclasses import replace
from typing import cast
from uuid import uuid4

from app.application.ports import StreamingProviderAdapter
from app.application.research_task_context import (
    ResearchReadObservation,
    ResearchReadRange,
    checkpoint_read_feedback,
    project_research_task_context,
)
from tests import test_research_handoff_runtime as handoff_tests


class PartialCheckpointResearcher(handoff_tests.CheckpointResearcher):
    async def stream(self, request, api_key, user_id):
        async for event in super().stream(request, api_key, user_id):
            call = event.tool_call
            if (
                call is not None
                and call.name == "research_checkpoint"
                and call.arguments.get("findings")
            ):
                event = replace(
                    event,
                    tool_call=replace(
                        call,
                        arguments={
                            **call.arguments,
                            "findings": cast(list[dict[str, object]], call.arguments["findings"])[
                                :1
                            ],
                        },
                    ),
                )
            yield event


class CheckpointFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def test_feedback_lists_every_source_in_a_normal_forty_read_batch(self):
        observations = tuple(
            ResearchReadObservation(
                str(uuid4()),
                index + 1,
                str(uuid4()),
                "a" * 64,
                "source_lines",
                (ResearchReadRange(index + 1000, index + 1009),),
            )
            for index in range(40)
        )
        response = checkpoint_read_feedback(observations)
        details = cast(list[dict[str, object]], response["undisposed_reads"])
        self.assertEqual(len(details), 40)
        self.assertEqual(response["undisposed_reads_omitted"], 0)
        for observation, detail in zip(observations, details, strict=True):
            self.assertEqual(detail["source_id"], observation.source_id)
            self.assertEqual(detail["content_hash"], observation.content_hash)
            self.assertEqual(
                detail["presented_ranges"],
                [
                    {
                        "start_line": observation.presented_ranges[0].start_line,
                        "end_line": observation.presented_ranges[0].end_line,
                    }
                ],
            )
        self.assertLess(len(json.dumps(response, separators=(",", ":")).encode()), 27_000)

    def test_extreme_metadata_is_bounded_without_cutting_source_ranges(self):
        ranges = tuple(ResearchReadRange(index * 2 + 1, index * 2 + 1) for index in range(200))
        observations = tuple(
            ResearchReadObservation(
                str(uuid4()),
                index + 1,
                str(uuid4()),
                "a" * 64,
                "source_lines",
                ranges,
            )
            for index in range(40)
        )
        response = checkpoint_read_feedback(observations)
        details = cast(list[dict[str, object]], response["undisposed_reads"])
        self.assertLessEqual(len(json.dumps(response, separators=(",", ":")).encode()), 27_000)
        self.assertEqual(response["undisposed_read_count"], 40)
        self.assertEqual(response["undisposed_reads_omitted"], 40 - len(details))
        self.assertGreater(cast(int, response["undisposed_reads_omitted"]), 0)
        self.assertTrue(details)
        self.assertEqual(
            details[0]["presented_ranges"],
            [{"start_line": window.start_line, "end_line": window.end_line} for window in ranges],
        )

    async def test_feedback_names_only_undisposed_reads_without_extending_retry_bound(self):
        run, records, _, pages, service, execution, coordinator = (
            handoff_tests.HandoffRuntimeTests().harness()
        )
        provider = PartialCheckpointResearcher()
        execution = replace(execution, provider=cast(StreamingProviderAdapter, provider))
        await service._run_research_task(run, execution, coordinator, records.task)
        invalid = [
            json.loads(message.content)
            for message in records.messages
            if message.role == "tool"
            and json.loads(message.content).get("status") == "invalid_request"
        ]
        self.assertEqual(len(invalid), 2)
        self.assertEqual(len(pages.body_reads), 2)
        for response in invalid:
            self.assertEqual(
                response["undisposed_reads"],
                [
                    {
                        "source_id": pages.body_reads[1],
                        "content_hash": "a" * 64,
                        "presented_ranges": [{"start_line": 1, "end_line": 1}],
                    }
                ],
            )
            self.assertEqual(response["undisposed_read_count"], 1)
            self.assertEqual(response["undisposed_reads_omitted"], 0)
            self.assertIn("Keep dispositions", response["reason"])
        self.assertEqual(records.task.checkpoint["stop_reason"], "research_checkpoint_invalid")
        corrective = [
            request
            for request in provider.requests
            if {tool.name for tool in request.tools} == {"research_checkpoint"}
        ]
        self.assertEqual(len(corrective), 2)
        projection = project_research_task_context(
            run.id, records.task.id, records.messages, records.tools, records.calls
        )
        self.assertEqual(len(projection.unprocessed_reads), 2)
        self.assertFalse(records.unknown)
