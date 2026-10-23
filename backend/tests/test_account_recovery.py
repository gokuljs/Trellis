import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from run_fixtures import MODEL, Provider, Repository

from app.application.errors import ApplicationError
from app.application.ports import StreamingProviderAdapter
from app.core.config import Settings
from app.domain.models import Session
from app.domain.recovery import RecoveryContract
from app.domain.runtime import RunSnapshot
from app.infrastructure.accounts import AccountRegistry, AccountRepository
from app.infrastructure.supabase_repository import VerifiedTokenSource
from app.infrastructure.workspace_grants import InstallationIdentity


class AccountDatabase(Repository):
    def __init__(self, user_id: UUID, token_source: VerifiedTokenSource) -> None:
        super().__init__()
        self.user_id = user_id
        self.token_source = token_source
        self.ensured = 0
        self.queries: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.operation: Callable[[], Awaitable[None]] | None = None
        self.error: ApplicationError | None = None

    async def ensure_account(self) -> None:
        self.ensured += 1

    async def list_recovery_candidates(self, installation_id: str) -> list[RunSnapshot]:
        self.queries.put_nowait((installation_id, self.token_source.get_token()))
        if self.operation is not None:
            await self.operation()
        if self.error is not None:
            raise self.error
        return []

    async def get_session(self, session_id: str) -> Session:
        return Session(session_id, str(self.user_id), "Chat", "now", "now", 0)

    async def list_models(self):
        return [MODEL]

    async def get_selected_model_id(self):
        return MODEL.id

    async def get_default_budget_preset(self):
        return "longer"

    def owns_run_lease(self, run_id: str) -> bool:
        return False

    async def create_run(self, *args, **kwargs) -> RunSnapshot:
        self.run = replace(self.run, recovery_contract=kwargs.get("recovery_contract"))
        return self.run


def registry_for(settings: Settings, client: httpx.AsyncClient):
    databases: dict[UUID, AccountDatabase] = {}

    def create_database(settings, user_id, token_source, client):
        assert user_id not in databases
        database = AccountDatabase(user_id, token_source)
        databases[user_id] = database
        return cast(AccountRepository, database)

    registry = AccountRegistry(
        settings,
        {},
        {MODEL.adapter_kind: cast(StreamingProviderAdapter, Provider())},
        cloud_client=client,
        repository_factory=create_database,
    )
    return registry, databases


def expiry():
    return datetime.now(UTC) + timedelta(hours=1)


def test_chat_only_accounts_receive_contracts_and_reuse_the_local_installation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        user_id = uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            context = await registry.get(user_id, access_token="first", expires_at=expiry())
            installation_id, token = await asyncio.wait_for(databases[user_id].queries.get(), 1)
            assert token == "first"
            assert context.workspace_bindings is None
            assert context.workspace_execution is None
            run = await context.run_service.create_run(str(uuid4()), "request", "Hello")
            assert isinstance(run.recovery_contract, RecoveryContract)
            assert run.recovery_contract.installation_id == installation_id
            assert run.recovery_contract.workspace is None
            assert run.recovery_contract.tool_versions == ()
            assert run.recovery_contract.adapter_versions == ((MODEL.adapter_kind, "v1"),)
            assert run.recovery_contract.request_identity == "request"
            await registry.close()
            replacement, restored_databases = registry_for(settings, client)
            await replacement.get(user_id, access_token="second", expires_at=expiry())
            restored_id, token = await asyncio.wait_for(
                restored_databases[user_id].queries.get(), 1
            )
            assert restored_id == installation_id
            assert token == "second"
            await replacement.close()

    asyncio.run(exercise())


def test_unverified_or_signed_out_accounts_do_not_discover_or_create_local_identity(tmp_path):
    async def exercise():
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            with pytest.raises(RuntimeError, match="fresh verified token"):
                await registry.get(uuid4())
            with pytest.raises(ValueError, match="expired"):
                await registry.get(
                    uuid4(),
                    access_token="expired",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            assert databases == {}
            assert not (tmp_path / "installation").exists()
            await registry.close()

    asyncio.run(exercise())


def test_parallel_activation_and_normal_requests_share_one_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        user_id = uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            contexts = await asyncio.gather(
                *(
                    registry.get(user_id, access_token="verified", expires_at=expiry())
                    for _ in range(5)
                )
            )
            assert all(context is contexts[0] for context in contexts)
            database = databases[user_id]
            await asyncio.wait_for(database.queries.get(), 1)
            for _ in range(5):
                await registry.get(user_id, access_token="verified", expires_at=expiry())
            await asyncio.sleep(0)
            assert database.ensured == 1
            assert database.queries.empty()
            await registry.close()

    asyncio.run(exercise())


def test_verified_refresh_restarts_rejected_discovery_without_rebuilding_account(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        user_id = uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            context = await registry.get(user_id, access_token="old", expires_at=expiry())
            database = databases[user_id]
            database.error = ApplicationError("cloud_access_denied", "expired")
            await asyncio.wait_for(database.queries.get(), 1)
            await asyncio.sleep(0)
            database.error = None
            restored = await registry.get(user_id, access_token="refreshed", expires_at=expiry())
            assert restored is context
            _identity, token = await asyncio.wait_for(database.queries.get(), 1)
            assert token == "refreshed"
            assert database.ensured == 1
            await registry.close()

    asyncio.run(exercise())


def test_registry_drains_discovery_before_closing_runtime_and_releasing_account(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        entered, drained, runtime_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        user_id = uuid4()
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            context = await registry.get(user_id, access_token="verified", expires_at=expiry())

            async def query():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    drained.set()

            databases[user_id].operation = query
            original_close = context.run_service.close

            async def close_runtime():
                assert drained.is_set()
                await original_close()
                runtime_closed.set()

            monkeypatch.setattr(context.run_service, "close", close_runtime)
            await asyncio.wait_for(entered.wait(), 1)
            await registry.close()
            assert runtime_closed.is_set()
            replacement, _ = registry_for(settings, client)
            await replacement.get(user_id, access_token="replacement", expires_at=expiry())
            await replacement.close()

    asyncio.run(exercise())


def test_accounts_share_installation_identity_but_keep_verified_discovery_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        first, second = uuid4(), uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            await asyncio.gather(
                registry.get(first, access_token="first-token", expires_at=expiry()),
                registry.get(second, access_token="second-token", expires_at=expiry()),
            )
            first_installation, first_token = await asyncio.wait_for(
                databases[first].queries.get(), 1
            )
            second_installation, second_token = await asyncio.wait_for(
                databases[second].queries.get(), 1
            )
            assert first_installation == second_installation
            assert (first_token, second_token) == ("first-token", "second-token")
            saved = InstallationIdentity.load(
                (tmp_path / "installation").resolve(), computer_id="a" * 64
            )
            assert saved.installation_id == first_installation
            await registry.close()

    asyncio.run(exercise())


def test_near_expiry_token_does_not_start_discovery_until_verified_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr("app.infrastructure.workspace_grants._computer_identity", lambda: "a" * 64)

    async def exercise():
        user_id = uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            context = await registry.get(
                user_id,
                access_token="near-expiry",
                expires_at=datetime.now(UTC) + timedelta(seconds=8),
            )
            await asyncio.sleep(0)
            assert databases[user_id].queries.empty()
            restored = await registry.get(user_id, access_token="fresh", expires_at=expiry())
            assert restored is context
            _installation, token = await asyncio.wait_for(databases[user_id].queries.get(), 1)
            assert token == "fresh"
            await registry.close()

    asyncio.run(exercise())


def test_cancelled_activation_drains_installation_storage_before_releasing_account(
    tmp_path, monkeypatch
):
    entered, released = threading.Event(), threading.Event()
    request_context = contextvars.ContextVar("installation_request_context", default="empty")

    def installation(data_dir):
        assert request_context.get() == "empty"
        entered.set()
        assert released.wait(2)
        return InstallationIdentity(str(uuid4()), "a" * 64)

    monkeypatch.setattr("app.infrastructure.accounts._installation_identity", installation)

    async def exercise():
        user_id = uuid4()
        settings = Settings(_env_file=None, environment="test", data_dir=tmp_path)
        async with httpx.AsyncClient() as client:
            registry, databases = registry_for(settings, client)
            request_context.set("private-request")
            activation = asyncio.create_task(
                registry.get(user_id, access_token="verified", expires_at=expiry())
            )
            assert await asyncio.to_thread(entered.wait, 1)
            activation.cancel()
            await asyncio.sleep(0)
            activation.cancel()
            closing = asyncio.create_task(registry.close())
            await asyncio.sleep(0)
            assert not activation.done()
            assert not closing.done()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await activation
            await closing
            assert databases[user_id].queries.empty()

    asyncio.run(exercise())
