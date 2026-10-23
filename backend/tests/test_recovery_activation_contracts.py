import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import BaseModel
from run_fixtures import MODEL, Provider, service
from test_reliable_acceptance import AcceptedRepository

from app.application.errors import ApplicationError
from app.application.ports import SessionRepository
from app.application.recovery_contracts import RecoveryContracts
from app.application.tools import ToolRegistration, ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.models import Session
from app.infrastructure.local_tools import LocalReadToolExecutor
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_grants import InstallationIdentity, WorkspaceGrantStore


def persistent_bindings(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    identity = InstallationIdentity.load(directory, computer_id="a" * 64)
    return LocalWorkspaceBindings(WorkspaceGrantStore(directory, str(uuid4()), identity))


def contracts(tmp_path, *, bindings=None, tools=None, installation=None):
    session = Session(str(uuid4()), str(uuid4()), "title", "now", "now", 0, str(tmp_path))
    sessions = SimpleNamespace(get_session=AsyncMock(return_value=session))
    factory = RecoveryContracts(
        installation or str(uuid4()),
        cast(SessionRepository, sessions),
        tools,
        bindings,
        RuntimeCapabilities(frozenset({"local_read"})),
        frozenset({MODEL.adapter_kind}),
    )
    return factory, session


def test_new_contracts_never_trust_a_cloud_path_without_explicit_folder_selection(tmp_path):
    async def scenario():
        bindings = persistent_bindings(tmp_path)
        factory, session = contracts(tmp_path, bindings=bindings)
        saved = await factory.create(session, "request", MODEL)
        assert saved.workspace is None
        await bindings.bind(session.id, tmp_path)
        trusted = await factory.create(session, "another-request", MODEL)
        assert trusted.workspace is not None
        assert trusted.workspace.canonical_path == str(tmp_path)
        assert trusted.workspace.directory_inode == tmp_path.stat().st_ino
        await bindings.bind(session.id, None)
        current = await factory.compatibility(
            replace(AcceptedRepository().run, session_id=session.id, recovery_contract=trusted)
        )
        assert trusted.incompatibility(current) == "workspace_unavailable"
        assert trusted.workspace is not None

    asyncio.run(scenario())


def test_only_offered_tools_and_selected_adapter_are_pinned_in_the_contract(tmp_path):
    async def scenario():
        bindings = persistent_bindings(tmp_path)
        tools = ToolRegistry(
            LocalReadToolExecutor(), capabilities=RuntimeCapabilities(frozenset({"local_read"}))
        )
        factory, session = contracts(tmp_path, bindings=bindings, tools=tools)
        without_folder = await factory.create(session, "request", MODEL)
        assert without_folder.tool_versions == ()
        assert without_folder.adapter_versions == ((MODEL.adapter_kind, "v1"),)
        await bindings.bind(session.id, tmp_path)
        with_folder = await factory.create(session, "request", MODEL)
        assert dict(with_folder.tool_versions) == {
            name: "v1" for name in ("read_file", "search_files", "list_files", "inspect_git")
        }
        assert with_folder.installation_id == factory.installation_id
        assert with_folder.request_identity == "request"

    asyncio.run(scenario())


def test_custom_tool_versions_participate_in_compatibility(tmp_path):
    class Arguments(BaseModel):
        pass

    class Executor:
        async def execute(self, name, arguments, workspace_root):
            return "saved", False

    async def scenario():
        def tools(version):
            return ToolRegistry(
                additional_tools=(
                    ToolRegistration(
                        "custom",
                        "A versioned tool",
                        Arguments,
                        Executor(),
                        recovery_version=version,
                    ),
                )
            )

        factory, session = contracts(tmp_path, tools=tools("v1"))
        saved = await factory.create(session, "request", MODEL)
        factory.tools = tools("v2")
        current = await factory.compatibility(
            replace(AcceptedRepository().run, session_id=session.id, recovery_contract=saved)
        )
        assert saved.incompatibility(current) == "incompatible_tool"

    asyncio.run(scenario())


@pytest.mark.parametrize("length,accepted", [(100, True), (101, False)])
def test_tool_recovery_versions_follow_the_persisted_contract_bound(length, accepted):
    class Arguments(BaseModel):
        pass

    class Executor:
        async def execute(self, name, arguments, workspace_root):
            return "saved", False

    registration = ToolRegistration(
        "custom", "Versioned", Arguments, Executor(), recovery_version="v" * length
    )
    if accepted:
        assert ToolRegistry(additional_tools=(registration,)).recovery_versions() == (
            ("custom", "v" * length),
        )
    else:
        with pytest.raises(ValueError, match="recovery version"):
            ToolRegistry(additional_tools=(registration,))


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_acceptance_retry_preserves_the_original_contract(tmp_path, monkeypatch, legacy):
    class Repository(AcceptedRepository):
        async def create_run(self, *args, **kwargs):
            self.run = replace(self.run, recovery_contract=kwargs.get("recovery_contract"))
            return await super().create_run(*args, **kwargs)

    async def scenario():
        repository = Repository()
        runtime = service(repository, Provider())
        factory, _ = contracts(tmp_path)
        creator = AsyncMock(side_effect=factory.create)
        runtime._recovery_contract_factory = None if legacy else creator
        monkeypatch.setattr(runtime, "_schedule", lambda _: None)
        first = await runtime.create_run(repository.run.session_id, "request", "hello")
        runtime._recovery_contract_factory = AsyncMock(side_effect=AssertionError("new contract"))
        retried = await runtime.create_run(repository.run.session_id, "request", "hello")
        assert retried.recovery_contract == first.recovery_contract
        assert (first.recovery_contract is None) is legacy
        assert repository.created == 1
        assert creator.await_count == (0 if legacy else 1)
        await runtime.close()

    asyncio.run(scenario())


def test_unavailable_adapter_rejects_contract_creation(tmp_path):
    async def scenario():
        factory, session = contracts(tmp_path)
        with pytest.raises(ApplicationError, match="unavailable"):
            await factory.create(session, "request", replace(MODEL, adapter_kind="removed"))

    asyncio.run(scenario())
