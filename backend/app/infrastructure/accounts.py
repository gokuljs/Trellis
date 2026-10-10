import asyncio
import errno
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no compatible process lock.
    fcntl = None  # type: ignore[assignment]

from app.application.chat import ChatService
from app.application.errors import ApplicationError
from app.application.onboarding import OnboardingService
from app.application.ports import ProviderAdapter, StreamingProviderAdapter
from app.application.profile import ProfileService
from app.application.runs import RunService
from app.application.sessions import SessionService
from app.application.settings import SettingsService
from app.application.tools import ToolRegistry
from app.core.config import Settings
from app.domain.models import ProviderName
from app.infrastructure.command_tools import LocalCommandToolExecutor
from app.infrastructure.database import Database
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
from app.infrastructure.runtime_events import RuntimeEventHub
from app.infrastructure.secrets import SecretStore
from app.infrastructure.sqlite_import import import_locked_account
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings


@dataclass(frozen=True, slots=True)
class AccountContext:
    database: Database | SupabaseRepository
    secret_store: SecretStore
    profile_service: ProfileService
    session_service: SessionService
    settings_service: SettingsService
    onboarding_service: OnboardingService
    chat_service: ChatService
    run_service: RunService
    event_hub: RuntimeEventHub


def _prepare_account_directory(data_dir: Path, user_id: UUID) -> Path:
    account_dir = data_dir / "accounts" / str(user_id)
    for path in (data_dir, data_dir / "accounts", account_dir):
        if path.is_symlink():
            raise RuntimeError("An account data path must not be a symlink")
        path.mkdir(mode=0o700, parents=path == data_dir, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("An account data path must be a private directory")
        metadata = path.stat()
        mode = metadata.st_mode
        if path == data_dir:
            if mode & (stat.S_IWGRP | stat.S_IWOTH) and not mode & stat.S_ISVTX:
                raise RuntimeError("Account data root is an unsafe shared directory")
            if metadata.st_uid != os.geteuid() and not (
                metadata.st_uid == 0
                and mode & stat.S_ISVTX
                and mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError("Account data root must be owned by this process")
        else:
            if stat.S_IMODE(mode) != 0o700:
                raise RuntimeError("An account data path must be a private directory")
            if metadata.st_uid != os.geteuid():
                raise RuntimeError("An account data path must be owned by this process")
    for filename in ("state.db", ".env"):
        if (account_dir / filename).is_symlink():
            raise RuntimeError("An account data file must not be a symlink")
    return account_dir


def _acquire_account_lock(account_dir: Path) -> int:
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("Per-account storage requires POSIX file locking")
    lock_path = account_dir / ".account.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("An account lock must be a private regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EACCES}:
                raise ApplicationError(
                    "account_in_use", "This account is open in another backend process."
                ) from None
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_unclaimed_lock(task: asyncio.Task[int]) -> None:
    try:
        descriptor = task.result()
    except BaseException:
        return
    os.close(descriptor)


class AccountRegistry:
    def __init__(
        self,
        settings: Settings,
        providers: Mapping[ProviderName, ProviderAdapter],
        runtime_providers: Mapping[str, StreamingProviderAdapter],
        *,
        tool_registry_factory: Callable[..., ToolRegistry] | None = None,
        cloud_client: httpx.AsyncClient | None = None,
        legacy_sqlite_for_tests: bool = False,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._runtime_providers = runtime_providers
        self._tool_registry_factory = tool_registry_factory
        self._cloud_client = cloud_client
        self._legacy_sqlite_for_tests = legacy_sqlite_for_tests
        self._token_sources: dict[UUID, VerifiedTokenSource] = {}
        self._contexts: dict[UUID, AccountContext] = {}
        self._locks: dict[UUID, int] = {}
        self._lock = asyncio.Lock()
        self._account_locks: dict[UUID, asyncio.Lock] = {}
        self._active_gets: dict[UUID, int] = {}
        self._gets_idle = asyncio.Event()
        self._gets_idle.set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def get(
        self,
        user_id: UUID,
        *,
        access_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> AccountContext:
        if not isinstance(user_id, UUID):
            raise TypeError("Account identity must be a verified UUID")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Account registry is closed")
            token_source: VerifiedTokenSource | None = None
            if not self._legacy_sqlite_for_tests:
                if access_token is None or expires_at is None:
                    raise RuntimeError("A fresh verified token is required for cloud data")
                token_source = self._token_sources.get(user_id)
                if token_source is None:
                    token_source = VerifiedTokenSource(user_id)
                    self._token_sources[user_id] = token_source
                token_source.record_verified(access_token, user_id, expires_at)
            existing = self._contexts.get(user_id)
            if existing is not None:
                return existing
            account_lock = self._account_locks.setdefault(user_id, asyncio.Lock())
            self._active_gets[user_id] = self._active_gets.get(user_id, 0) + 1
            self._gets_idle.clear()

        try:
            async with account_lock:
                if self._closed:
                    raise RuntimeError("Account registry is closed")
                existing = self._contexts.get(user_id)
                if existing is not None:
                    return existing

                account_dir = await asyncio.to_thread(
                    _prepare_account_directory, self._settings.data_dir, user_id
                )
                lock_task = asyncio.create_task(
                    asyncio.to_thread(_acquire_account_lock, account_dir)
                )
                try:
                    lock_descriptor = await asyncio.shield(lock_task)
                except BaseException:
                    lock_task.add_done_callback(_release_unclaimed_lock)
                    raise
                try:
                    database: Database | SupabaseRepository
                    if self._legacy_sqlite_for_tests:
                        local_database = Database(account_dir / "state.db", owner_id=user_id)
                        await local_database.initialize()
                        await asyncio.to_thread(local_database.path.chmod, 0o600)
                        await local_database.recover_active_runs()
                        database = local_database
                    else:
                        if self._cloud_client is None or token_source is None:
                            raise RuntimeError("A verified cloud client is required")
                        cloud_database = SupabaseRepository(
                            self._settings, user_id, token_source, self._cloud_client
                        )
                        await import_locked_account(
                            account_dir, user_id, lock_descriptor, cloud_database
                        )
                        await cloud_database.ensure_account()
                        database = cloud_database
                    secret_store = SecretStore(account_dir / ".env")
                    event_hub = RuntimeEventHub()
                    workspace_bindings = LocalWorkspaceBindings()
                    tools = (
                        self._tool_registry_factory(database)
                        if self._tool_registry_factory is not None
                        else ToolRegistry(
                            LocalReadToolExecutor(),
                            patch_executor=LocalPatchToolExecutor(),
                            command_executor=LocalCommandToolExecutor(),
                        )
                    )
                    run_service = RunService(
                        database,
                        database,
                        database,
                        database,
                        secret_store,
                        self._runtime_providers,
                        event_hub,
                        tool_registry=tools,
                        workspace_guidance_reader=read_workspace_guidance,
                        workspace_bindings=workspace_bindings,
                        lease_repository=database
                        if isinstance(database, SupabaseRepository)
                        else None,
                    )
                    try:
                        await run_service.resume_decided_approvals()
                        context = AccountContext(
                            database=database,
                            secret_store=secret_store,
                            profile_service=ProfileService(database),
                            session_service=SessionService(
                                database, workspace_bindings=workspace_bindings
                            ),
                            settings_service=SettingsService(database, secret_store),
                            onboarding_service=OnboardingService(database, database, secret_store),
                            chat_service=ChatService(database, secret_store, self._providers),
                            run_service=run_service,
                            event_hub=event_hub,
                        )
                    except BaseException:
                        await run_service.close()
                        raise
                except BaseException:
                    os.close(lock_descriptor)
                    raise
                self._contexts[user_id] = context
                self._locks[user_id] = lock_descriptor
                if self._closed:
                    raise RuntimeError("Account registry is closed")
                return context
        finally:
            remaining = self._active_gets[user_id] - 1
            if remaining:
                self._active_gets[user_id] = remaining
            else:
                del self._active_gets[user_id]
                self._account_locks.pop(user_id, None)
                if user_id not in self._contexts:
                    self._token_sources.pop(user_id, None)
            if not self._active_gets:
                self._gets_idle.set()

    async def close(self) -> None:
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._finish_close())
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _finish_close(self) -> None:
        await self._gets_idle.wait()
        async with self._lock:
            contexts = tuple(self._contexts.values())
            self._contexts.clear()
            self._token_sources.clear()
            locks = tuple(self._locks.values())
            self._locks.clear()
        try:
            results = await asyncio.gather(
                *(context.run_service.close() for context in contexts), return_exceptions=True
            )
        finally:
            for descriptor in locks:
                os.close(descriptor)
        for result in results:
            if isinstance(result, BaseException):
                raise result
