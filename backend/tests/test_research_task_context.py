import json
import unittest
from dataclasses import replace
from uuid import uuid4

from app.application.research_task_context import (
    bounded_research_notebook,
    checkpoint_disposes_reads,
    parse_research_read_observations,
    project_research_task_context,
)
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelToolCall,
    RunMessageRecord,
    ToolCallRecord,
    ToolCallStatus,
)

RUN, TASK, OTHER_TASK, SOURCE = (str(uuid4()) for _ in range(4))
HASH = "a" * 64
NOW = "2026-10-31T00:00:00Z"


def exchange(index: int, *, checkpoint: int | None = None, complete=True, task=TASK):
    model_id, assistant_id, tool_id = (str(uuid4()) for _ in range(3))
    name = "research_checkpoint" if checkpoint is not None else "web_source_read"
    provider_id = f"tool-{index}"
    request: dict[str, object] = {
        "research": {"task_id": task, "read_through_ordinal": checkpoint or 0}
    }
    call = ModelCallRecord(
        model_id,
        RUN,
        index,
        "openai",
        "model",
        "adapter",
        ModelCallStatus.COMPLETED,
        request,
        None,
        None,
        None,
        100,
        100,
        None,
        None,
        None,
        0.001,
        None,
        None,
        NOW,
        NOW,
        task_id=task,
    )
    assistant = RunMessageRecord(
        assistant_id,
        RUN,
        index * 10 + 1,
        "assistant",
        "",
        model_id,
        None,
        NOW,
        (ModelToolCall(provider_id, name, {}),),
        task_id=task,
    )
    tool = ToolCallRecord(
        tool_id,
        RUN,
        assistant_id,
        0,
        provider_id,
        name,
        {},
        ToolCallStatus.COMPLETED if complete else ToolCallStatus.PENDING,
        None,
        None,
        NOW,
        NOW if complete else None,
        task_id=task,
    )
    value = (
        {"status": "saved", "revision": index}
        if checkpoint is not None
        else {
            "status": "success",
            "source_id": SOURCE,
            "content_hash": HASH,
            "read_kind": "source_lines",
            "substantive": True,
            "presented_ranges": [{"start_line": index, "end_line": index}],
            "untrusted_lines": [{"line": index, "text": f"Evidence {index}: " + "X" * 8000}],
        }
    )
    result = RunMessageRecord(
        str(uuid4()),
        RUN,
        index * 10 + 2,
        "tool",
        json.dumps(value),
        None,
        tool_id,
        NOW,
        task_id=task,
    )
    return [assistant, result] if complete else [assistant], [tool], [call]


def combine(*exchanges):
    return tuple([item for group in exchanges for item in group[index]] for index in range(3))


class ResearchTaskContextTests(unittest.TestCase):
    def project(self, *exchanges):
        messages, tools, calls = combine(*exchanges)
        return project_research_task_context(RUN, TASK, messages, tools, calls)

    def test_two_actual_reads_require_checkpoint_and_preserve_exact_references(self):
        result = self.project(exchange(1), exchange(2))
        self.assertTrue(result.checkpoint_required)
        self.assertEqual(len(result.unprocessed_reads), 2)
        self.assertEqual(result.read_through_ordinal, 22)
        self.assertEqual(result.checkpointed_read_through_ordinal, 0)
        self.assertEqual(
            result.observed_reads[0].to_dict(),
            {
                "tool_call_id": result.messages[1].tool_call_id,
                "message_ordinal": 12,
                "source_id": SOURCE,
                "content_hash": HASH,
                "read_kind": "source_lines",
                "presented_ranges": [{"start_line": 1, "end_line": 1}],
            },
        )

    def test_successful_checkpoint_uses_frozen_request_cursor_not_tool_arguments(self):
        checkpoint = exchange(3, checkpoint=22)
        checkpoint[1][0] = replace(checkpoint[1][0], arguments={"read_through_ordinal": 9999})
        result = self.project(exchange(1), exchange(2), checkpoint)
        self.assertFalse(result.checkpoint_required)
        self.assertEqual(result.unprocessed_reads, ())
        self.assertEqual(result.checkpointed_read_through_ordinal, 22)

    def test_failed_or_unsaved_checkpoint_does_not_advance_processing_cursor(self):
        for variant in ("tool_failed", "not_saved", "model_incomplete"):
            with self.subTest(variant=variant):
                checkpoint = exchange(3, checkpoint=22)
                if variant == "tool_failed":
                    checkpoint[1][0] = replace(checkpoint[1][0], status=ToolCallStatus.FAILED)
                elif variant == "not_saved":
                    checkpoint[0][1] = replace(checkpoint[0][1], content='{"status":"error"}')
                else:
                    checkpoint[2][0] = replace(checkpoint[2][0], status=ModelCallStatus.STREAMING)
                result = self.project(exchange(1), exchange(2), checkpoint)
                self.assertTrue(result.checkpoint_required)
                self.assertEqual(result.checkpointed_read_through_ordinal, 0)

    def test_checkpoint_in_read_batch_cannot_consume_reads_from_same_response(self):
        checkpoint = exchange(3, checkpoint=22)
        new_read = exchange(4)
        checkpoint[0][0] = replace(
            checkpoint[0][0],
            tool_calls=(*checkpoint[0][0].tool_calls, *new_read[0][0].tool_calls),
        )
        new_read[1][0] = replace(new_read[1][0], assistant_message_id=checkpoint[0][0].id)
        new_read[0][1] = replace(new_read[0][1], ordinal=33)
        checkpoint[0].append(new_read[0][1])
        checkpoint[1].extend(new_read[1])
        result = self.project(exchange(1), exchange(2), checkpoint)
        self.assertEqual([read.message_ordinal for read in result.unprocessed_reads], [33])
        self.assertEqual(result.read_through_ordinal, 33)
        self.assertEqual(result.checkpointed_read_through_ordinal, 22)

    def test_compaction_drops_checkpointed_large_prefix_but_keeps_recent_two_exchanges(self):
        reads = tuple(exchange(index) for index in range(1, 7))
        messages, _, _ = combine(*reads, exchange(7, checkpoint=62))
        audit_contents = [message.content for message in messages]
        result = self.project(*reads, exchange(7, checkpoint=62))
        self.assertEqual([message.ordinal for message in result.messages], [61, 62, 71, 72])
        self.assertNotIn("Evidence 1:", "".join(message.content for message in result.messages))
        self.assertEqual([message.content for message in messages], audit_contents)
        self.assertEqual(len(result.observed_reads), 6)
        self.assertEqual(result.unprocessed_reads, ())

    def test_unprocessed_evidence_and_open_pairs_survive_compaction(self):
        result = self.project(
            exchange(1),
            exchange(2),
            exchange(3, checkpoint=22),
            exchange(4),
            exchange(5, complete=False),
        )
        self.assertTrue(result.has_open_tools)
        self.assertIn(41, [message.ordinal for message in result.messages])
        self.assertIn(42, [message.ordinal for message in result.messages])
        self.assertIn(51, [message.ordinal for message in result.messages])
        self.assertEqual([read.message_ordinal for read in result.unprocessed_reads], [42])

    def test_compaction_never_splits_an_exchange_with_an_unfinished_sibling_tool(self):
        first = exchange(1)
        unfinished = exchange(2, complete=False)
        first[0][0] = replace(
            first[0][0],
            tool_calls=(*first[0][0].tool_calls, *unfinished[0][0].tool_calls),
        )
        first[1].append(replace(unfinished[1][0], assistant_message_id=first[0][0].id))
        result = self.project(first, exchange(3), exchange(4), exchange(5, checkpoint=42))
        self.assertTrue(result.has_open_tools)
        self.assertIn(first[0][0], result.messages)
        self.assertIn(first[0][1], result.messages)
        self.assertEqual(result.messages[0].tool_calls, first[0][0].tool_calls)

    def test_other_task_data_is_never_counted_or_returned(self):
        own = exchange(1)
        foreign = exchange(2, task=OTHER_TASK)
        result = self.project(own, foreign)
        self.assertEqual(result.messages, tuple(own[0]))
        self.assertEqual(len(result.observed_reads), 1)
        self.assertFalse(result.checkpoint_required)

    def test_preview_and_forged_ranges_are_not_substantive_reads(self):
        for changes in (
            {"substantive": False},
            {"read_kind": "outline"},
            {"presented_ranges": [{"start_line": 1, "end_line": 2}]},
            {"content_hash": "not-a-hash"},
        ):
            with self.subTest(changes=changes):
                item = exchange(1)
                value = json.loads(item[0][1].content)
                value.update(changes)
                item[0][1] = replace(item[0][1], content=json.dumps(value))
                result = self.project(item)
                self.assertEqual(result.observed_reads, ())
                self.assertFalse(result.checkpoint_required)

    def test_future_or_foreign_cursor_is_rejected(self):
        for cursor, metadata_task in ((42, TASK), (22, OTHER_TASK)):
            with self.subTest(cursor=cursor, task=metadata_task):
                checkpoint = exchange(3, checkpoint=cursor)
                checkpoint[2][0] = replace(
                    checkpoint[2][0],
                    request_snapshot={
                        "research": {"task_id": metadata_task, "read_through_ordinal": cursor}
                    },
                )
                with self.assertRaises(ValueError):
                    self.project(exchange(1), exchange(2), checkpoint)

    def test_replaying_projection_is_deterministic_and_keeps_unprocessed_hashes(self):
        transcript = combine(exchange(1), exchange(2, checkpoint=12), exchange(3))
        first = project_research_task_context(RUN, TASK, *transcript)
        replay = project_research_task_context(RUN, TASK, *transcript)
        self.assertEqual(first, replay)
        self.assertEqual(first.unprocessed_reads[0].content_hash, HASH)

    def test_handoff_requires_a_finding_or_explicit_gap_for_every_read(self):
        reads = self.project(exchange(1), exchange(2)).unprocessed_reads
        finding = {
            "id": "finding",
            "text": "A supported conclusion",
            "citations": [{"source_id": SOURCE, "start_line": 1, "end_line": 1}],
        }
        gap = {
            "id": "gap",
            "text": "The second excerpt does not contain evaluation details",
            "status": "open",
            "citations": [{"source_id": SOURCE, "start_line": 2, "end_line": 2}],
        }
        self.assertFalse(checkpoint_disposes_reads({}, reads))
        self.assertFalse(checkpoint_disposes_reads({"findings": [finding]}, reads))
        self.assertTrue(checkpoint_disposes_reads({"findings": [finding], "gaps": [gap]}, reads))
        self.assertFalse(
            checkpoint_disposes_reads(
                {
                    "gaps": [
                        {
                            **gap,
                            "citations": [
                                {"source_id": str(uuid4()), "start_line": 1, "end_line": 2}
                            ],
                        }
                    ]
                },
                reads,
            )
        )

    def test_notebook_keeps_complete_evidence_and_explicitly_omits_optional_records(self):
        citation = {"source_id": SOURCE, "start_line": 1200, "end_line": 1220}
        state = {
            "objective": "Compare the actual evaluation results",
            "checkpoint": {
                "findings": [{"id": "current", "text": "local result", "citations": [citation]}],
                "examined_source_ids": [SOURCE],
            },
            "research_brief": {
                "brief": {
                    "scope": "Methods and results",
                    "source_restrictions": ["Primary papers"],
                },
                "subquestions": [{"id": "q1", "question": "Are the evaluations comparable?"}],
                "findings": [{"id": "root", "text": "Y" * 1800, "citations": [citation]}],
            },
            "worker_findings": [
                {"findings": [{"id": "worker", "text": "Z" * 1800, "citations": [citation]}]}
            ],
        }
        original = json.dumps(state, sort_keys=True)
        value: dict = bounded_research_notebook(state, max_bytes=1500)
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False).encode()), 1500)
        self.assertEqual(value["objective"], state["objective"])
        self.assertEqual(value["research_brief"]["brief"], state["research_brief"]["brief"])
        self.assertEqual(
            value["research_brief"]["subquestions"], state["research_brief"]["subquestions"]
        )
        self.assertEqual(value["checkpoint"]["findings"][0]["citations"], [citation])
        self.assertEqual(value["checkpoint"]["findings"][0]["text"], "local result")
        self.assertEqual(value["omitted_counts"]["findings"], 2)
        self.assertEqual(json.dumps(state, sort_keys=True), original)

    def test_notebook_never_truncates_mandatory_user_constraints(self):
        with self.assertRaisesRegex(ValueError, "mandatory"):
            bounded_research_notebook(
                {
                    "objective": "Question",
                    "research_brief": {
                        "brief": {"scope": "Scope", "source_restrictions": ["X" * 4000]}
                    },
                },
                max_bytes=1000,
            )

    def test_canonical_root_questions_win_over_stale_task_questions(self):
        value: dict = bounded_research_notebook(
            {
                "objective": "Compare",
                "checkpoint": {"subquestions": [{"id": "same", "question": "Stale question"}]},
                "research_brief": {
                    "subquestions": [{"id": "same", "question": "Current question"}]
                },
            }
        )
        self.assertEqual(value["research_brief"]["subquestions"][0]["question"], "Current question")
        self.assertNotIn("subquestions", value["checkpoint"])

    def test_snapshot_read_metadata_round_trips_and_rejects_corruption(self):
        observation = self.project(exchange(1)).observed_reads[0]
        self.assertEqual(parse_research_read_observations([observation.to_dict()]), (observation,))
        for changes in (
            {"message_ordinal": True},
            {"source_id": "foreign"},
            {"content_hash": "wrong"},
            {"presented_ranges": [{"start_line": 2, "end_line": 1}]},
            {"presented_ranges": [{"start_line": 1, "end_line": 201}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_research_read_observations([{**observation.to_dict(), **changes}])


if __name__ == "__main__":
    unittest.main()
