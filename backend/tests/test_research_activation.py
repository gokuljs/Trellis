import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from pydantic import BaseModel

from app.application.context_preview import ContextPreviewService
from app.application.errors import ApplicationError
from app.application.ports import (
    ContextCheckpointRepository,
    ProfileRepository,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
)
from app.application.research import RESEARCH_WEB_NAMES, ResearchReportFinalizer, research_web_names
from app.application.research_ports import ResearchRepository
from app.application.runs import RunService
from app.application.tools import ToolRegistration, ToolRegistry
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import ModelDescriptor, Session
from app.domain.recovery import RecoveryContract
from app.domain.research import LEGACY_RESEARCH_POLICY, RESEARCH_POLICY_V2
from app.domain.runtime import RunStatus
from app.infrastructure.accounts import AccountRegistry, AccountRepository
from app.infrastructure.research_repository import ResearchRepositoryMixin
from app.infrastructure.runtime_events import RuntimeEventHub
from tests.test_research_agent_loop import make_run


class ReadinessRepository(ResearchRepositoryMixin):
    def __init__(self, value):
        self.value = value

    async def _request(self, method, path, *, params=None, payload=None):
        if method != "POST" or path != "rpc/research_readiness":
            raise AssertionError("Unexpected readiness operation")
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class ResearchReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_complete_current_policy_enables_new_research(self):
        complete = {"schema_version": 2, "policy_version": RESEARCH_POLICY_V2, "ready": True}
        self.assertTrue(await ReadinessRepository(complete).research_ready())
        for value in (
            {"schema_version": 1, "ready": True},
            {**complete, "ready": False},
            {**complete, "policy_version": LEGACY_RESEARCH_POLICY},
            {**complete, "schema_version": True},
            {**complete, "schema_version": 3},
            {"schema_version": 2, "ready": True},
            ApplicationError("database_unavailable", "Temporarily unavailable"),
        ):
            with self.subTest(value=value):
                self.assertFalse(await ReadinessRepository(value).research_ready())

    async def test_legacy_client_disables_new_creation_after_policy_activation(self):
        with patch(
            "app.infrastructure.research_repository.CURRENT_RESEARCH_POLICY", LEGACY_RESEARCH_POLICY
        ):
            self.assertTrue(
                await ReadinessRepository({"schema_version": 1, "ready": True}).research_ready()
            )
            self.assertFalse(
                await ReadinessRepository(
                    {"schema_version": 2, "policy_version": RESEARCH_POLICY_V2, "ready": True}
                ).research_ready()
            )

    async def test_registration_waits_for_the_complete_research_path(self):
        database = AsyncMock()
        database.web_tools_ready.return_value = True
        registry = AccountRegistry(
            Settings(_env_file=None, enabled_capabilities=frozenset({"web_read"})), {}, {}
        )
        evidence = WebEvidenceService(
            cast(WebRepository, database), cast(SessionRepository, database), owner_id=str(uuid4())
        )
        try:
            for ready in (False, True):
                database.research_ready.return_value = ready
                registrations = await registry._web_tool_registrations(
                    cast(AccountRepository, database), evidence, cast(RuntimeEventHub, Mock())
                )
                names = {registration.name for registration in registrations}
                self.assertIn("web_source", names)
                self.assertEqual(
                    {"web_source_read", "web_source_outline", "web_source_find"}.issubset(names),
                    ready,
                )
        finally:
            await registry.close()


class EmptyArguments(BaseModel):
    pass


class ResearchToolAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def harness(self, mode="chat", policy=LEGACY_RESEARCH_POLICY, *, include_research_tools=True):
        run = replace(
            make_run(), status=RunStatus.QUEUED, run_mode=mode, research_policy_version=policy
        )
        owner = str(uuid4())
        session = Session(
            run.session_id, owner, "Research tools", run.created_at, run.created_at, 0
        )
        model = ModelDescriptor(
            run.model_id,
            run.provider_id,
            "OpenAI",
            run.adapter_kind,
            run.upstream_model_id,
            "Selected model",
            False,
            True,
            True,
            True,
        )
        records = AsyncMock()
        records.get_session.return_value = session
        records.get_profile.return_value = SimpleNamespace(id=owner)
        records.list_models.return_value = [model]
        records.match_run_request.return_value = None
        records.create_run.return_value = run
        records.research_ready.return_value = True
        records.get.return_value = "fixture-key"
        names = RESEARCH_WEB_NAMES | research_web_names(RESEARCH_POLICY_V2)
        if not include_research_tools:
            names = RESEARCH_WEB_NAMES
        registry = ToolRegistry(
            additional_tools=tuple(
                ToolRegistration(name, "Read evidence", EmptyArguments, Mock())
                for name in sorted(names)
            )
        )
        contract = RecoveryContract(
            1,
            "checkpoint-runtime-v1",
            ((run.adapter_kind, "v1"),),
            tuple((name, "web-reading-v1") for name in sorted(names)),
            "request",
            str(uuid4()),
            None,
        )
        factory = AsyncMock(return_value=contract)
        service = RunService(
            cast(RunRepository, records),
            cast(SessionRepository, records),
            cast(SettingsRepository, records),
            cast(ProfileRepository, records),
            cast(SecretStorePort, records),
            {run.adapter_kind: cast(StreamingProviderAdapter, Mock())},
            tool_registry=registry,
            capabilities=RuntimeCapabilities(frozenset({"web_search", "web_read"})),
            research_repository=cast(ResearchRepository, records),
            research_reports=cast(ResearchReportFinalizer, Mock()),
            recovery_contract_factory=factory,
        )
        return run, records, service

    async def test_chat_and_legacy_research_keep_their_original_tool_sets(self):
        for mode, policy, expected in (
            ("chat", LEGACY_RESEARCH_POLICY, RESEARCH_WEB_NAMES),
            ("research", LEGACY_RESEARCH_POLICY, RESEARCH_WEB_NAMES),
            ("research", RESEARCH_POLICY_V2, research_web_names(RESEARCH_POLICY_V2)),
        ):
            with self.subTest(mode=mode, policy=policy):
                run, _records, service = self.harness(mode, policy)
                context = await service._execution_context(run)
                self.assertEqual({tool.name for tool in context.tools}, expected)

    async def test_chat_recovery_contract_keeps_only_original_tools(self):
        for mode, expected in (
            ("chat", RESEARCH_WEB_NAMES),
            ("research", research_web_names(RESEARCH_POLICY_V2)),
        ):
            with self.subTest(mode=mode):
                run, records, service = self.harness(mode, RESEARCH_POLICY_V2)
                with patch.object(service, "_schedule"):
                    await service.create_run(
                        run.session_id,
                        "request",
                        "Compare behavior",
                        model_id=run.model_id,
                        budget_preset="conservative",
                        run_mode=mode,
                    )
                contract = records.create_run.await_args.kwargs["recovery_contract"]
                self.assertEqual({name for name, _version in contract.tool_versions}, expected)
                if mode == "chat":
                    self.assertNotIn("run_mode", records.create_run.await_args.kwargs)
                    records.research_ready.assert_not_awaited()

    async def test_complete_storage_cannot_admit_research_with_an_old_tool_registry(self):
        run, records, service = self.harness(
            "research", RESEARCH_POLICY_V2, include_research_tools=False
        )
        with self.assertRaises(ApplicationError) as rejected:
            await service.create_run(
                run.session_id,
                "request",
                "Compare behavior",
                model_id=run.model_id,
                budget_preset="conservative",
                run_mode="research",
            )
        self.assertEqual(rejected.exception.code, "research_unavailable")
        records.create_run.assert_not_awaited()

    async def test_chat_draft_preview_counts_only_the_tools_chat_can_use(self):
        previews = []
        for include_research_tools in (False, True):
            run, records, service = self.harness(include_research_tools=include_research_tools)
            model = replace(records.list_models.return_value[0], adapter_kind="openai")
            records.list_catalog_models.return_value = [model]
            preview = ContextPreviewService(
                cast(SessionRepository, records),
                cast(SettingsRepository, records),
                cast(ContextCheckpointRepository, records),
                cast(SecretStorePort, records),
                available_adapters=frozenset({"openai"}),
                tool_registry=service._tool_registry,
                capabilities=RuntimeCapabilities(frozenset({"web_search", "web_read"})),
            )
            previews.append(
                await preview.preview(
                    model_id=run.model_id,
                    draft="What is a database index?",
                    budget_preset="conservative",
                )
            )
        self.assertEqual(previews[0].status, "ready")
        self.assertIsNotNone(previews[0].input_tokens)
        self.assertEqual(previews[0].input_tokens, previews[1].input_tokens)


if __name__ == "__main__":
    unittest.main()
