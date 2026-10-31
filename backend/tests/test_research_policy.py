import unittest
from dataclasses import replace
from datetime import datetime
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.application.errors import ApplicationError
from app.application.research import control_specs, merge_checkpoint
from app.application.research_notebook import notebook_coverage
from app.application.runs import RunService
from app.core.config import Settings
from app.domain.models import ModelDescriptor
from app.domain.research import LEGACY_RESEARCH_POLICY, RESEARCH_POLICY_V2, research_limits
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from tests.test_research_agent_loop import make_run


def question() -> dict:
    return {
        "id": "methods",
        "question": "How do the methods differ?",
        "status": "addressed",
        "evidence_requirements": [
            {"id": "methods-text", "description": "Read the actual method", "depth": "full_text"}
        ],
    }


def finding() -> dict:
    return {
        "id": "a-method",
        "text": "The authors describe a role-based workflow.",
        "question_ids": ["methods"],
        "requirement_ids": ["methods-text"],
        "citations": [{"source_id": str(uuid4()), "start_line": 500, "end_line": 510}],
        "support_kind": "author_reported",
        "evidence_depth": "full_text",
    }


class ResearchPolicyTests(unittest.TestCase):
    def test_extended_time_is_versioned_without_changing_other_allowances(self) -> None:
        old = research_limits("longer")
        new = research_limits("longer", RESEARCH_POLICY_V2)
        self.assertEqual(old.seconds, 1200)
        self.assertEqual(new.seconds, 2700)
        self.assertEqual(replace(new, seconds=1200), old)
        self.assertEqual(research_limits("conservative", RESEARCH_POLICY_V2).seconds, 600)

    def test_unsupported_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            research_limits("longer", "future-policy")

    def test_collection_reserve_uses_frozen_duration(self) -> None:
        run = replace(
            make_run(),
            budget_preset="longer",
            execution_budget_seconds=1200,
            research_policy_version=RESEARCH_POLICY_V2,
        )
        scope = RunService._research_execution_scope(run, finalization=False)
        self.assertEqual(
            (
                datetime.fromisoformat(run.deadline_at) - datetime.fromisoformat(scope.deadline_at)
            ).total_seconds(),
            300,
        )
        new = replace(run, execution_budget_seconds=2700)
        scope = RunService._research_execution_scope(new, finalization=False)
        self.assertEqual(
            (
                datetime.fromisoformat(new.deadline_at) - datetime.fromisoformat(scope.deadline_at)
            ).total_seconds(),
            675,
        )

    def test_v1_checkpoint_schema_stays_unchanged(self) -> None:
        legacy = control_specs("worker", LEGACY_RESEARCH_POLICY)[0].input_schema
        current = control_specs("worker", RESEARCH_POLICY_V2)[0].input_schema
        self.assertNotIn("brief", cast(dict[str, object], legacy["properties"]))
        self.assertIn("brief", cast(dict[str, object], current["properties"]))
        self.assertEqual(
            merge_checkpoint({}, {"gaps": ["Missing methods"]})["gaps"], ["Missing methods"]
        )

    def test_addressed_flag_without_evidence_is_missing(self) -> None:
        state = merge_checkpoint(
            {}, {"subquestions": [question()]}, policy_version=RESEARCH_POLICY_V2
        )
        self.assertEqual(notebook_coverage(state)[0]["status"], "missing")

    def test_findings_require_question_and_source_links(self) -> None:
        for change in ({"question_ids": []}, {"citations": []}, {"requirement_ids": ["invented"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_checkpoint(
                    {},
                    {"subquestions": [question()], "findings": [{**finding(), **change}]},
                    policy_version=RESEARCH_POLICY_V2,
                )

    def test_recorded_evidence_is_not_equated_with_reviewed_coverage(self) -> None:
        state = merge_checkpoint(
            {},
            {"subquestions": [question()], "findings": [finding()]},
            policy_version=RESEARCH_POLICY_V2,
        )
        self.assertEqual(notebook_coverage(state)[0]["status"], "evidence_recorded")

    def test_resolved_gap_replaces_stale_gap_and_preserves_frozen_context(self) -> None:
        previous: dict[str, object] = {
            "conversation_context": [{"role": "user", "content": "Compare these papers"}],
            "subquestions": [question()],
        }
        gap = {"id": "methods-gap", "text": "Methods missing", "question_ids": ["methods"]}
        initial = merge_checkpoint(previous, {"gaps": [gap]}, policy_version=RESEARCH_POLICY_V2)
        resolved = merge_checkpoint(
            initial,
            {"gaps": [{**gap, "status": "resolved"}]},
            policy_version=RESEARCH_POLICY_V2,
        )
        gaps = cast(list[dict[str, object]], resolved["gaps"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["status"], "resolved")
        self.assertEqual(resolved["conversation_context"], previous["conversation_context"])
        with self.assertRaises(ValueError):
            merge_checkpoint(
                initial, {"conversation_context": []}, policy_version=RESEARCH_POLICY_V2
            )

    def test_worker_can_link_findings_to_lead_questions(self) -> None:
        state = merge_checkpoint(
            {},
            {"findings": [finding()]},
            policy_version=RESEARCH_POLICY_V2,
            known_questions=[question()],
        )
        self.assertEqual(len(cast(list[dict[str, object]], state["findings"])), 1)


class ResearchPolicyTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_authentication_ttl_uses_policy_duration(self) -> None:
        user = uuid4()
        settings = Settings(
            _env_file=None,
            supabase_url="https://fixture.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        )
        model = ModelDescriptor(
            "openai:gpt-5.5",
            "openai",
            "OpenAI",
            "openai_responses",
            "gpt-5.5",
            "GPT",
            True,
            True,
            True,
            True,
        )
        async with httpx.AsyncClient() as client:
            repository = SupabaseRepository(settings, user, VerifiedTokenSource(user), client)
            for mode, expected in (("research", 2730), ("chat", 1230)):
                with (
                    self.subTest(mode=mode),
                    patch(
                        "app.infrastructure.supabase_repository.CURRENT_RESEARCH_POLICY",
                        RESEARCH_POLICY_V2,
                    ),
                    patch.object(VerifiedTokenSource, "valid_for", return_value=False) as valid,
                ):
                    with self.assertRaises(ApplicationError) as raised:
                        await repository.create_run(
                            str(uuid4()),
                            str(uuid4()),
                            str(uuid4()),
                            "Compare methods",
                            model,
                            budget_preset="longer",
                            run_mode=mode,
                        )
                    self.assertEqual(raised.exception.code, "run_token_too_short")
                    valid.assert_called_once_with(expected)
