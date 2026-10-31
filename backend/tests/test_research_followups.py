import unittest
from dataclasses import replace
from typing import cast
from uuid import uuid4

from app.application.research import (
    ResearchCoordinator,
    collection_guidance,
    control_specs,
    research_instructions,
)
from app.application.research_notebook import collection_basis, merge_notebook
from app.application.research_ports import ResearchRepository
from app.application.research_task_context import ResearchReadObservation, ResearchReadRange
from app.domain.research import (
    RESEARCH_POLICY_V2,
    ResearchPhase,
    ResearchSnapshot,
    ResearchTask,
    ResearchTaskStatus,
)
from tests.test_research_agent_loop import make_run
from tests.test_research_policy import finding, question


def snapshot(run, checkpoint: dict, tasks: tuple = ()) -> ResearchSnapshot:
    return ResearchSnapshot(
        run.id,
        run.session_id,
        str(uuid4()),
        ResearchPhase.RESEARCHING,
        "Compare methods",
        1,
        checkpoint,
        tasks,
        None,
        500,
        policy_version=RESEARCH_POLICY_V2,
        execution_budget_seconds=600,
    )


def assignment(**updates) -> dict:
    return {
        "objective": "Read the methods section and compare workflow design",
        "question_ids": ["methods"],
        "requirement_ids": ["methods-text"],
        "conflict_ids": [],
        "purpose": "fill_gap",
        "expected_value": "The methods are still missing from the comparison.",
        **updates,
    }


def saved_read(saved_finding: dict) -> dict[str, object]:
    citation = saved_finding["citations"][0]
    return ResearchReadObservation(
        str(uuid4()),
        10,
        citation["source_id"],
        "a" * 64,
        "source_lines",
        (ResearchReadRange(citation["start_line"], citation["end_line"]),),
    ).to_dict()


class FollowupGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.execution = replace(make_run(), research_policy_version=RESEARCH_POLICY_V2)

    def test_worker_local_finding_ids_cannot_hide_or_supply_sibling_coverage(self) -> None:
        for first_depth, second_depth, missing in (
            ("full_text", "listing", []),
            ("abstract", "full_text", ["methods-text"]),
        ):
            with self.subTest(first_depth=first_depth, second_depth=second_depth):
                first = {**finding(), "id": "f1", "evidence_depth": first_depth}
                second = {
                    **finding(),
                    "id": "f1",
                    "question_ids": ["other"],
                    "requirement_ids": ["other-evidence"],
                    "evidence_depth": second_depth,
                }
                other = {
                    "id": "other",
                    "question": "What does the other source establish?",
                    "evidence_requirements": [
                        {
                            "id": "other-evidence",
                            "description": "Other evidence",
                            "depth": second_depth,
                        }
                    ],
                }
                workers = tuple(
                    ResearchTask(
                        str(uuid4()),
                        self.execution.id,
                        "Read assigned evidence",
                        "worker",
                        1,
                        ResearchTaskStatus.COMPLETED,
                        1,
                        {"findings": [saved]},
                    )
                    for saved in (first, second)
                )
                state = replace(
                    snapshot(self.execution, {"subquestions": [question(), other]}, workers),
                    source_reads=(saved_read(first), saved_read(second)),
                )
                guidance = collection_guidance(state, self.execution)
                self.assertEqual(
                    [item["id"] for item in guidance["unresolved_requirements"]], missing
                )

    def test_addressed_flag_without_evidence_still_needs_followup(self) -> None:
        state = snapshot(self.execution, {"subquestions": [question()]})
        guidance = collection_guidance(state, self.execution)
        self.assertEqual(guidance["next_action"], "research")
        self.assertEqual(guidance["unresolved_requirements"][0]["id"], "methods-text")

    def test_unavailable_facet_does_not_block_other_required_evidence(self) -> None:
        requested = question()
        requested["evidence_requirements"].append(
            {"id": "results", "description": "Read evaluation results", "depth": "full_text"}
        )
        state = snapshot(
            self.execution,
            {
                "subquestions": [requested],
                "gaps": [
                    {
                        "id": "methods-blocked",
                        "text": "Methods are inaccessible",
                        "question_ids": ["methods"],
                        "requirement_ids": ["methods-text"],
                        "status": "unavailable",
                        "citations": [],
                    }
                ],
            },
        )
        guidance = collection_guidance(state, self.execution)
        self.assertEqual(guidance["next_action"], "research")
        self.assertEqual([item["id"] for item in guidance["unresolved_requirements"]], ["results"])
        self.assertEqual(guidance["unavailable_requirements"][0]["id"], "methods-text")

    def test_resolved_root_issue_does_not_reappear_from_old_worker_notes(self) -> None:
        issue = {
            "id": "methods-gap",
            "text": "Missing methods",
            "question_ids": ["methods"],
            "status": "open",
        }
        worker = ResearchTask(
            str(uuid4()),
            self.execution.id,
            "Read methods",
            "worker",
            1,
            ResearchTaskStatus.COMPLETED,
            1,
            {"gaps": [issue]},
        )
        state = snapshot(
            self.execution,
            {"subquestions": [question()], "gaps": [{**issue, "status": "resolved"}]},
            (worker,),
        )
        self.assertEqual(collection_guidance(state, self.execution)["open_gaps"], [])

    def test_finalization_and_round_limits_stop_additional_collection(self) -> None:
        state = snapshot(self.execution, {"subquestions": [question()]})
        for budget in ({"collection_calls_remaining": 0}, {"budget_unknown": True}):
            with self.subTest(budget=budget):
                self.assertEqual(
                    collection_guidance(state, self.execution, shared_budget=budget)["next_action"],
                    "stop",
                )
        worker = ResearchTask(
            str(uuid4()),
            self.execution.id,
            "Last allowed round",
            "worker",
            2,
            ResearchTaskStatus.COMPLETED,
            1,
            {},
        )
        self.assertEqual(
            collection_guidance(replace(state, tasks=(worker,)), self.execution)["reason"],
            "collection_round_limit",
        )

    def test_low_value_decision_is_invalidated_by_new_evidence_or_clarification(self) -> None:
        checkpoint: dict[str, object] = {
            "subquestions": [question()],
            "collection_decision": {
                "reason": "low_expected_value",
                "explanation": "Repeated official-source searches found no methods.",
            },
        }
        checkpoint["collection_decision_basis"] = collection_basis(checkpoint)
        state = snapshot(self.execution, checkpoint)
        self.assertEqual(collection_guidance(state, self.execution)["reason"], "low_expected_value")
        changed = {**checkpoint, "clarifications": [{"text": "Include the full paper"}]}
        self.assertEqual(
            collection_guidance(replace(state, checkpoint=changed), self.execution)["next_action"],
            "research",
        )
        worker = ResearchTask(
            str(uuid4()),
            self.execution.id,
            "New evidence",
            "worker",
            1,
            ResearchTaskStatus.COMPLETED,
            1,
            {"findings": [finding()]},
        )
        self.assertNotEqual(
            collection_guidance(replace(state, tasks=(worker,)), self.execution)["reason"],
            "low_expected_value",
        )

    def test_evidence_records_do_not_claim_semantic_review(self) -> None:
        state = snapshot(self.execution, {"subquestions": [question()], "findings": [finding()]})
        guidance = collection_guidance(state, self.execution, observed_reads=None)
        self.assertEqual(guidance["reason"], "coverage_recorded")
        self.assertIs(guidance["requires_report_review"], True)

    def test_issue_links_cannot_invent_requirements(self) -> None:
        with self.assertRaises(ValueError):
            merge_notebook(
                {"subquestions": [question()]},
                {
                    "gaps": [
                        {
                            "id": "fake",
                            "text": "No source",
                            "question_ids": ["methods"],
                            "requirement_ids": ["invented"],
                            "status": "unavailable",
                        }
                    ]
                },
            )

    def test_cited_evidence_must_have_been_presented_in_complete_ranges(self) -> None:
        saved_finding = finding()
        source = saved_finding["citations"][0]["source_id"]
        state = snapshot(
            self.execution, {"subquestions": [question()], "findings": [saved_finding]}
        )
        read = ResearchReadObservation(
            str(uuid4()),
            10,
            source,
            "a" * 64,
            "source_lines",
            (ResearchReadRange(500, 504), ResearchReadRange(506, 510)),
        )
        incomplete = collection_guidance(state, self.execution, observed_reads=[read])
        self.assertEqual(incomplete["unresolved_requirements"][0]["status"], "read_needed")
        self.assertEqual(incomplete["coverage"][0]["status"], "missing")
        complete = replace(read, presented_ranges=(ResearchReadRange(500, 510),))
        self.assertEqual(
            collection_guidance(state, self.execution, observed_reads=[complete])["reason"],
            "coverage_recorded",
        )

    def test_abstract_claim_does_not_satisfy_a_full_text_requirement(self) -> None:
        saved_finding = {**finding(), "evidence_depth": "abstract"}
        state = snapshot(
            self.execution, {"subquestions": [question()], "findings": [saved_finding]}
        )
        self.assertEqual(
            collection_guidance(state, self.execution)["unresolved_requirements"][0]["status"],
            "read_needed",
        )

    def test_production_guidance_requires_saved_read_provenance(self) -> None:
        saved_finding = finding()
        state = snapshot(
            self.execution, {"subquestions": [question()], "findings": [saved_finding]}
        )
        self.assertEqual(collection_guidance(state, self.execution)["next_action"], "research")
        observed = replace(state, source_reads=(saved_read(saved_finding),))
        guidance = collection_guidance(observed, self.execution)
        self.assertEqual(guidance["reason"], "coverage_recorded")
        self.assertIn(saved_finding["citations"][0]["source_id"], guidance["examined_source_ids"])

    def test_all_explicitly_unavailable_requirements_stop_with_disclosure(self) -> None:
        state = snapshot(
            self.execution,
            {
                "subquestions": [question()],
                "gaps": [
                    {
                        "id": "methods-blocked",
                        "text": "No in-scope full text is available",
                        "question_ids": ["methods"],
                        "requirement_ids": ["methods-text"],
                        "status": "unavailable",
                        "citations": [],
                    }
                ],
            },
        )
        guidance = collection_guidance(state, self.execution)
        self.assertEqual(guidance["reason"], "evidence_unavailable")
        self.assertEqual(guidance["unavailable_requirements"][0]["id"], "methods-text")

    def test_brief_entries_and_merged_question_count_remain_bounded(self) -> None:
        with self.assertRaises(ValueError):
            merge_notebook(
                {}, {"brief": {"scope": "Compare papers", "source_restrictions": ["X" * 2001]}}
            )
        previous: dict[str, object] = {
            "subquestions": [
                {
                    **question(),
                    "id": f"q{index}",
                    "evidence_requirements": [
                        {
                            "id": f"r{index}",
                            "description": "Read evidence",
                            "depth": "source_excerpt",
                        }
                    ],
                }
                for index in range(12)
            ]
        }
        with self.assertRaises(ValueError):
            merge_notebook(previous, {"subquestions": [question()]})


class MemoryFollowups:
    def __init__(self, state: ResearchSnapshot) -> None:
        self.state = state
        self.created = []

    async def read_research(self, run_id):
        return self.state

    async def checkpoint_research(
        self, run_id, generation, task_id, revision, operation_id, checkpoint
    ):
        task = next(item for item in self.state.tasks if item.id == task_id)
        saved = replace(task, revision=revision + 1, checkpoint=checkpoint)
        self.state = replace(
            self.state,
            tasks=tuple(saved if item.id == task_id else item for item in self.state.tasks),
        )
        return saved

    async def create_research_tasks(self, run_id, generation, operation_id, tasks):
        self.created.extend(tasks)
        created = tuple(
            ResearchTask(
                item.id,
                run_id,
                item.objective,
                item.role,
                item.round_index,
                ResearchTaskStatus.COMPLETED,
                0,
                {},
            )
            for item in tasks
        )
        self.state = replace(self.state, tasks=(*self.state.tasks, *created))
        return created


class FocusedDelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.execution = replace(make_run(), research_policy_version=RESEARCH_POLICY_V2)
        self.lead = ResearchTask(
            str(uuid4()),
            self.execution.id,
            "Compare methods",
            "lead",
            0,
            ResearchTaskStatus.RUNNING,
            0,
            {},
        )
        self.repository = MemoryFollowups(snapshot(self.execution, {"subquestions": [question()]}))

        async def unused(task):
            raise AssertionError("The memory repository returns completed tasks")

        self.coordinator = ResearchCoordinator(
            cast(ResearchRepository, self.repository), self.execution, unused
        )

    async def delegate(self, items: list[dict], *, round_index: int = 1, operation_id=None):
        return await self.coordinator.control(
            self.lead,
            "delegate_research",
            {
                "round_index": round_index,
                "assignments": items,
            },
            operation_id or str(uuid4()),
        )

    async def test_focused_assignment_contains_evidence_and_scope_without_forcing_fulltext(
        self,
    ) -> None:
        requested = question()
        requested["evidence_requirements"][0]["depth"] = "abstract"
        self.repository.state = replace(
            self.repository.state,
            checkpoint={
                "subquestions": [requested],
                "brief": {
                    "scope": "Compare abstracts",
                    "source_restrictions": ["Only the two supplied arXiv abstract pages"],
                },
                "examined_source_ids": [str(uuid4())],
            },
        )
        result = await self.delegate([assignment()])
        self.assertEqual(result["status"], "complete")
        objective = self.repository.created[0].objective
        self.assertIn("methods-text", objective)
        self.assertIn("Depth: abstract", objective)
        self.assertIn("Only the two supplied arXiv abstract pages", objective)
        self.assertNotIn("Depth: full_text", objective)
        self.assertIn("unresolved_requirements", result["collection_guidance"])

    async def test_later_round_rejects_broad_unlinked_or_already_covered_assignment(self) -> None:
        previous = ResearchTask(
            str(uuid4()),
            self.execution.id,
            "First round",
            "worker",
            1,
            ResearchTaskStatus.COMPLETED,
            1,
            {},
        )
        supported = finding()
        self.repository.state = replace(
            self.repository.state,
            tasks=(previous,),
            checkpoint={"subquestions": [question()], "findings": [supported]},
            source_reads=(saved_read(supported),),
        )
        result = await self.delegate([assignment()], round_index=2)
        self.assertEqual(result["status"], "invalid_request")
        result = await self.delegate([assignment(requirement_ids=[])], round_index=2)
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(self.repository.created, [])

    async def test_corroboration_is_allowed_with_specific_evidence_and_expected_value(self) -> None:
        supported = finding()
        self.repository.state = replace(
            self.repository.state,
            checkpoint={"subquestions": [question()], "findings": [supported]},
            source_reads=(saved_read(supported),),
        )
        result = await self.delegate(
            [
                assignment(
                    purpose="corroborate",
                    expected_value="An independent original source can test the author's claim.",
                )
            ]
        )
        self.assertEqual(result["status"], "complete")

    async def test_duplicate_batch_work_is_rejected(self) -> None:
        result = await self.delegate([assignment(), assignment()])
        self.assertEqual(result["status"], "invalid_request")

    async def test_initial_wave_covers_all_frameworks_with_shared_question_ids(self) -> None:
        requested = question()
        requested["evidence_requirements"] = [
            {"id": name, "description": f"Read {name} methods", "depth": "full_text"}
            for name in ("autogen", "metagpt", "chatdev")
        ]
        self.repository.state = replace(
            self.repository.state, checkpoint={"subquestions": [requested]}
        )
        result = await self.delegate(
            [assignment(requirement_ids=["autogen"]), assignment(requirement_ids=["metagpt"])]
        )
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("chatdev", result["reason"])
        self.assertEqual(self.repository.created, [])
        result = await self.delegate(
            [
                assignment(requirement_ids=["autogen"]),
                assignment(requirement_ids=["metagpt", "chatdev"]),
            ]
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.repository.created), 2)
        self.assertIn("chatdev", self.repository.created[1].objective)

    async def test_later_wave_prioritizes_never_assigned_requirements(self) -> None:
        await self.delegate([assignment()])
        requested = question()
        requested["evidence_requirements"].append(
            {"id": "chatdev", "description": "Read ChatDev methods", "depth": "full_text"}
        )
        self.repository.state = replace(
            self.repository.state, checkpoint={"subquestions": [requested]}
        )
        guidance = collection_guidance(self.repository.state, self.execution)
        self.assertEqual(guidance["unassigned_requirement_ids"], ["chatdev"])
        self.assertEqual(guidance["unresolved_requirements"][0]["id"], "chatdev")
        result = await self.delegate([assignment()], round_index=2)
        self.assertEqual(result["status"], "invalid_request")
        result = await self.delegate([assignment(requirement_ids=["chatdev"])], round_index=2)
        self.assertEqual(result["status"], "complete")

    async def assigned_question_without_findings(self) -> None:
        requested = question()
        requested["evidence_requirements"].append(
            {"id": "results", "description": "Read results", "depth": "full_text"}
        )
        other = {
            "id": "other",
            "question": "What does the other framework do?",
            "evidence_requirements": [
                {"id": "other-methods", "description": "Read other methods", "depth": "full_text"}
            ],
        }
        self.repository.state = replace(
            self.repository.state, checkpoint={"subquestions": [requested, other]}
        )
        initial = await self.delegate(
            [
                assignment(requirement_ids=["methods-text", "results"]),
                assignment(question_ids=["other"], requirement_ids=["other-methods"]),
            ]
        )
        self.assertEqual(initial["status"], "complete")
        supported = finding()
        self.repository.state = replace(
            self.repository.state,
            checkpoint={"subquestions": [requested, other], "findings": [supported]},
            source_reads=(saved_read(supported),),
        )

    async def test_assignment_without_findings_does_not_hide_an_unanswered_question(self) -> None:
        await self.assigned_question_without_findings()
        guidance = collection_guidance(self.repository.state, self.execution)
        self.assertEqual(guidance["unassigned_requirement_ids"], [])
        self.assertEqual(guidance["unresolved_requirements"][0]["id"], "other-methods")
        self.assertEqual(guidance["unanswered_question_ids"], ["other"])
        rejected = await self.delegate([assignment(requirement_ids=["results"])], round_index=2)
        self.assertEqual(rejected["status"], "invalid_request")
        self.assertIn("other", rejected["reason"])
        accepted = await self.delegate(
            [assignment(question_ids=["other"], requirement_ids=["other-methods"])], round_index=2
        )
        self.assertEqual(accepted["status"], "complete")

    async def test_conflict_assignment_cannot_exempt_other_workers_from_unanswered_priority(self):
        await self.assigned_question_without_findings()
        self.repository.state = replace(
            self.repository.state,
            checkpoint={
                **self.repository.state.checkpoint,
                "conflicts": [
                    {
                        "id": "method-conflict",
                        "text": "Two methods disagree",
                        "question_ids": ["methods"],
                        "requirement_ids": ["methods-text"],
                        "status": "open",
                    }
                ],
            },
        )
        conflict = assignment(
            requirement_ids=[], conflict_ids=["method-conflict"], purpose="check_conflict"
        )
        mixed = await self.delegate(
            [assignment(requirement_ids=["results"]), conflict], round_index=2
        )
        self.assertEqual(mixed["status"], "invalid_request")
        self.assertIn("other", mixed["reason"])
        pure = await self.delegate([conflict], round_index=2)
        self.assertEqual(pure["status"], "complete")

    async def test_more_than_twenty_four_initial_requirements_remain_bounded(self) -> None:
        questions = [
            {
                "id": f"q{group}",
                "question": f"Compare dimension {group}",
                "evidence_requirements": [
                    {
                        "id": f"q{group}-r{index}",
                        "description": "Read the original source",
                        "depth": "full_text",
                    }
                    for index in range(9)
                ],
            }
            for group in range(3)
        ]
        self.repository.state = replace(
            self.repository.state, checkpoint={"subquestions": questions}
        )
        result = await self.delegate([assignment(question_ids=["q0"], requirement_ids=["q0-r0"])])
        self.assertEqual(result["status"], "complete")
        guidance = result["collection_guidance"]
        self.assertEqual(len(guidance["unassigned_requirement_ids"]), 26)
        self.assertEqual(guidance["unassigned_question_ids"], ["q1", "q2"])

    async def test_large_complete_requirement_text_can_use_later_waves(self) -> None:
        questions = [
            {
                "id": f"q{group}",
                "question": f"Compare dimension {group}",
                "evidence_requirements": [
                    {
                        "id": f"q{group}-r{index}",
                        "description": "Evidence details " + "x" * 1980,
                        "depth": "full_text",
                    }
                    for index in range(9)
                ],
            }
            for group in range(2)
        ]
        self.repository.state = replace(
            self.repository.state, checkpoint={"subquestions": questions}
        )
        result = await self.delegate([assignment(question_ids=["q0"], requirement_ids=["q0-r0"])])
        self.assertEqual(result["status"], "complete")
        self.assertLessEqual(len(self.repository.created[0].objective), 16000)
        self.assertEqual(len(result["collection_guidance"]["unassigned_requirement_ids"]), 17)

    async def test_worker_schema_only_advertises_valid_notebook_fields(self) -> None:
        worker = next(
            item
            for item in control_specs("worker", RESEARCH_POLICY_V2)
            if item.name == "research_checkpoint"
        )
        lead = next(
            item
            for item in control_specs("lead", RESEARCH_POLICY_V2)
            if item.name == "research_checkpoint"
        )
        self.assertEqual(
            set(cast(dict[str, object], worker.input_schema["properties"])),
            {"phase", "findings", "examined_source_ids", "gaps", "conflicts", "stop_reason"},
        )
        definitions = cast(dict[str, object], worker.input_schema.get("$defs", {}))
        self.assertNotIn("ResearchBrief", definitions)
        self.assertNotIn("ResearchQuestion", definitions)
        self.assertNotIn("scope", worker.description)
        self.assertIn("brief", cast(dict[str, object], lead.input_schema["properties"]))
        legacy = next(
            item for item in control_specs("worker") if item.name == "research_checkpoint"
        )
        self.assertIn("scope", cast(dict[str, object], legacy.input_schema["properties"]))

    async def test_worker_preserves_pending_v2_empty_scope_defaults(self) -> None:
        worker = replace(self.lead, role="worker")
        self.repository.state = replace(self.repository.state, tasks=(worker,))

        result = await self.coordinator.control(
            worker,
            "research_checkpoint",
            {"scope": "", "brief": None, "subquestions": [], "collection_decision": None},
            str(uuid4()),
        )
        self.assertEqual(result["status"], "saved")
        result = await self.coordinator.control(
            worker, "research_checkpoint", {"scope": "Broaden the scope"}, str(uuid4())
        )
        self.assertEqual(result["status"], "invalid_request")

    async def test_v2_instructions_request_complete_minimal_citations(self) -> None:
        instructions = research_instructions(self.lead, RESEARCH_POLICY_V2)
        self.assertIn("minimal complete", instructions)
        self.assertIn("Split compound findings", instructions)
        self.assertIn("unassigned", instructions)

    async def test_replay_retains_original_assignment_when_source_inventory_changes(self) -> None:
        operation_id = str(uuid4())
        first = await self.delegate([assignment()], operation_id=operation_id)
        original = self.repository.created[0].objective
        self.repository.state = replace(
            self.repository.state,
            checkpoint={
                **self.repository.state.checkpoint,
                "examined_source_ids": [str(uuid4())],
            },
        )
        replay = await self.delegate([assignment()], operation_id=operation_id)
        self.assertEqual(first["status"], replay["status"])
        self.assertEqual(len(self.repository.created), 1)
        self.assertEqual(self.repository.created[0].objective, original)

    async def test_legacy_delegation_schema_is_unchanged(self) -> None:
        legacy = next(item for item in control_specs("lead") if item.name == "delegate_research")
        current = next(
            item
            for item in control_specs("lead", RESEARCH_POLICY_V2)
            if item.name == "delegate_research"
        )
        self.assertIn("objectives", cast(dict[str, object], legacy.input_schema["properties"]))
        self.assertNotIn("assignments", cast(dict[str, object], legacy.input_schema["properties"]))
        self.assertIn("assignments", cast(dict[str, object], current.input_schema["properties"]))
