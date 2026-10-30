import asyncio
import contextvars
import errno
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no compatible process lock.
    fcntl = None  # type: ignore[assignment]

from app.application.chat import ChatService
from app.application.context_preview import ContextPreviewService
from app.application.errors import ApplicationError
from app.application.onboarding import OnboardingService
from app.application.ports import (
    ChatRepository,
    ContextCheckpointRepository,
    OnboardingRepository,
    ProviderAdapter,
    RecoveryRepository,
    RunLeasePort,
    RunRepository,
    SecretStorePort,
    StreamingProviderAdapter,
)
from app.application.profile import ProfileService
from app.application.recovery_contracts import RecoveryContracts
from app.application.recovery_discovery import RecoveryDiscovery
from app.application.run_capacity import RunCapacity
from app.application.runs import RunService
from app.application.secret_redaction import SecretRedactor
from app.application.sessions import SessionService
from app.application.settings import SettingsService
from app.application.tool_context import ToolExecutionContext
from app.application.tools import ToolRegistration, ToolRegistry
from app.application.web_evidence import WebEvidenceService
from app.application.web_ports import WebRepository
from app.application.web_tools import WebToolService
from app.core.config import Settings
from app.domain.capabilities import RuntimeCapabilities, RuntimeCapability
from app.domain.models import ProviderName
from app.domain.runtime import RunEventType
from app.infrastructure.brave_search import BraveSearchProvider
from app.infrastructure.command_tools import LocalCommandToolExecutor
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor, read_workspace_guidance
from app.infrastructure.retrieval_worker import (
    DockerPublicRetriever,
    GatewayControlClient,
    RetrievalConcurrency,
    WorkerDeployment,
)
from app.infrastructure.runtime_events import RuntimeEventHub
from app.infrastructure.server_credentials import ServerCredentialStore
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_execution import CommandMarkerStore, LocalWorkspaceExecution
from app.infrastructure.workspace_grants import (
    InstallationIdentity,
    WorkspaceGrantStore,
)


class AccountRepository(
    ChatRepository,
    ContextCheckpointRepository,
    OnboardingRepository,
    RecoveryRepository,
    RunRepository,
    RunLeasePort,
    WebRepository,
    Protocol,
):
    async def ensure_account(self) -> None: ...


RepositoryFactory = Callable[
    [Settings, UUID, VerifiedTokenSource, httpx.AsyncClient], AccountRepository
]


@dataclass(frozen=True, slots=True)
class AccountContext:
    database: AccountRepository
    secret_store: SecretStorePort
    profile_service: ProfileService
    session_service: SessionService
    settings_service: SettingsService
    onboarding_service: OnboardingService
    context_preview_service: ContextPreviewService
    chat_service: ChatService
    run_service: RunService
    event_hub: RuntimeEventHub
    web_evidence_service: WebEvidenceService | None = None
    workspace_bindings: LocalWorkspaceBindings | None = None
    workspace_execution: LocalWorkspaceExecution | None = None
    recovery_discovery: RecoveryDiscovery | None = None


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
    return account_dir.resolve(strict=True)


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


def _installation_identity(data_dir: Path) -> InstallationIdentity:
    try:
        directory = data_dir.resolve(strict=True) / "installation"
        directory.mkdir(mode=0o700, exist_ok=True)
        return InstallationIdentity.load(directory)
    except OSError, RuntimeError, ValueError:
        raise ApplicationError(
            "installation_identity_unavailable",
            "Trellis could not securely load its local recovery identity. "
            "Check local storage and try again.",
        ) from None


class AccountRegistry:
    def __init__(
        self,
        settings: Settings,
        providers: Mapping[ProviderName, ProviderAdapter],
        runtime_providers: Mapping[str, StreamingProviderAdapter],
        *,
        tool_registry_factory: Callable[..., ToolRegistry] | None = None,
        cloud_client: httpx.AsyncClient | None = None,
        repository_factory: RepositoryFactory | None = None,
    ) -> None:
        self._settings = settings
        self._run_capacity = RunCapacity(
            max_active_runs=settings.max_active_runs,
            max_active_runs_per_user=settings.max_active_runs_per_user,
        )
        self._web_concurrency = RetrievalConcurrency()
        self._capabilities = RuntimeCapabilities(settings.enabled_capabilities)
        self._credentials = ServerCredentialStore(settings)
        self._redactor = SecretRedactor(
            *(
                key.get_secret_value().strip()
                for key in (
                    settings.openai_api_key,
                    settings.anthropic_api_key,
                    settings.brave_search_api_key,
                    settings.web_gateway_admin_secret,
                )
                if key is not None
            )
        )
        self._providers = providers
        self._runtime_providers = runtime_providers
        self._tool_registry_factory = tool_registry_factory
        self._cloud_client = cloud_client
        self._repository_factory: RepositoryFactory = repository_factory or (
            lambda settings, user_id, source, client: SupabaseRepository(
                settings, user_id, source, client
            )
        )
        self._token_sources: dict[UUID, VerifiedTokenSource] = {}
        self._contexts: dict[UUID, AccountContext] = {}
        self._locks: dict[UUID, int] = {}
        self._lock = asyncio.Lock()
        self._account_locks: dict[UUID, asyncio.Lock] = {}
        self._active_gets: dict[UUID, int] = {}
        self._gets_idle = asyncio.Event()
        self._gets_idle.set()
        self._installation: InstallationIdentity | None = None
        self._installation_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def _web_tool_registrations(
        self,
        database: AccountRepository,
        evidence: WebEvidenceService,
        event_hub: RuntimeEventHub,
    ) -> tuple[ToolRegistration, ...]:
        if not self._capabilities.enabled.intersection({"web_read", "web_search", "web_crawl"}):
            return ()
        if not await database.web_tools_ready():
            return ()
        settings = self._settings
        try:
            search = BraveSearchProvider(
                settings.brave_search_api_key.get_secret_value().strip()
                if settings.brave_search_api_key is not None
                else None
            )
        except ValueError:
            search = BraveSearchProvider(None)
        retriever = None
        renderer = None
        crawler = None
        if (
            self._capabilities.enabled.intersection({"web_read", "web_crawl"})
            and settings.web_worker_image is not None
            and settings.web_gateway_image is not None
            and settings.web_gateway_secret_file is not None
            and settings.web_gateway_admin_secret is not None
        ):
            try:
                deployment = WorkerDeployment(
                    settings.web_worker_image,
                    settings.web_gateway_image,
                    settings.web_gateway_container,
                    settings.web_gateway_ipc_volume,
                    settings.web_gateway_secret_file,
                )
                candidate = DockerPublicRetriever(
                    deployment,
                    GatewayControlClient(
                        settings.web_gateway_control_url,
                        settings.web_gateway_admin_secret.get_secret_value().strip(),
                    ),
                    concurrency=self._web_concurrency,
                )
                async with asyncio.timeout(15):
                    if await candidate.is_ready():
                        retriever = candidate
            except ValueError, TimeoutError:
                pass
            if retriever is not None and self._capabilities.allows("web_crawl"):
                try:
                    async with asyncio.timeout(15):
                        if await retriever.is_crawl_ready() is True:
                            crawler = retriever
                except ValueError, TimeoutError:
                    pass
            if retriever is not None and self._capabilities.allows("web_read"):
                try:
                    async with asyncio.timeout(15):
                        if await retriever.is_render_ready() is True:
                            renderer = retriever
                except ValueError, TimeoutError:
                    pass

        async def publish(context: ToolExecutionContext, metadata: dict[str, object]) -> None:
            await context.assert_active()
            event = await database.append_run_event(
                context.run_id,
                RunEventType.PHASE,
                {**metadata, "tool_call_id": context.tool_call_id},
            )
            await event_hub.publish(event)

        service = WebToolService(
            evidence,
            retriever=retriever,
            renderer=renderer,
            crawler=crawler,
            search=search if search.configured else None,
            search_key_hash=search.key_hash,
            monthly_search_limit=settings.brave_search_monthly_limit,
            redactor=self._redactor,
            publish_progress=publish,
        )
        source_capability: RuntimeCapability = (
            "web_read"
            if self._capabilities.allows("web_read")
            else "web_crawl"
            if self._capabilities.allows("web_crawl")
            else "web_search"
        )
        return service.registrations(
            search_ready=search.configured and self._capabilities.allows("web_search"),
            read_ready=retriever is not None and self._capabilities.allows("web_read"),
            render_ready=renderer is not None,
            crawl_ready=crawler is not None,
            source_capability=source_capability,
        )

    async def _get_installation(self) -> InstallationIdentity:
        async with self._installation_lock:
            if self._installation is None:
                task = asyncio.create_task(
                    asyncio.to_thread(_installation_identity, self._settings.data_dir),
                    context=contextvars.Context(),
                )
                try:
                    self._installation = await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not task.cancelled():
                        task.exception()
                    raise
            return self._installation

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
            if access_token is None or expires_at is None:
                raise RuntimeError("A fresh verified token is required for cloud data")
            token_source = self._token_sources.get(user_id)
            if token_source is None:
                token_source = VerifiedTokenSource(user_id)
                self._token_sources[user_id] = token_source
            token_source.record_verified(access_token, user_id, expires_at)
            existing = self._contexts.get(user_id)
            if existing is not None:
                if existing.recovery_discovery is not None:
                    existing.recovery_discovery.activate(
                        authentication_revision=token_source.revision
                    )
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
                    if existing.recovery_discovery is not None:
                        existing.recovery_discovery.activate(
                            authentication_revision=token_source.revision
                        )
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
                    if self._cloud_client is None:
                        raise RuntimeError("A verified cloud client is required")
                    database = self._repository_factory(
                        self._settings, user_id, token_source, self._cloud_client
                    )
                    await database.ensure_account()
                    secret_store = self._credentials
                    event_hub = RuntimeEventHub()
                    web_evidence = WebEvidenceService(database, database, owner_id=str(user_id))
                    installation = await self._get_installation()
                    workspace_bindings = None
                    workspace_execution = None
                    if self._capabilities.workspace_enabled:
                        workspace_bindings = LocalWorkspaceBindings(
                            WorkspaceGrantStore(account_dir, str(user_id), installation)
                        )
                        workspace_execution = LocalWorkspaceExecution(
                            workspace_bindings,
                            CommandMarkerStore(
                                self._settings.data_dir.resolve(strict=True) / "installation",
                                installation,
                            ),
                            str(user_id),
                        )
                    tools = (
                        self._tool_registry_factory(database)
                        if self._tool_registry_factory is not None
                        else ToolRegistry(
                            LocalReadToolExecutor()
                            if self._capabilities.allows("local_read")
                            else None,
                            patch_executor=LocalPatchToolExecutor()
                            if self._capabilities.allows("local_patch")
                            else None,
                            command_executor=LocalCommandToolExecutor()
                            if self._capabilities.allows("local_command")
                            else None,
                            capabilities=self._capabilities,
                            redactor=self._redactor,
                            additional_tools=await self._web_tool_registrations(
                                database, web_evidence, event_hub
                            ),
                        )
                    )
                    recovery_contracts = RecoveryContracts(
                        installation_id=installation.installation_id,
                        sessions=database,
                        tools=tools,
                        bindings=workspace_bindings,
                        capabilities=self._capabilities,
                        adapter_kinds=frozenset(self._runtime_providers),
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
                        capabilities=self._capabilities,
                        redactor=self._redactor,
                        web_repository=database,
                        workspace_guidance_reader=read_workspace_guidance
                        if self._capabilities.allows("local_read")
                        else None,
                        workspace_bindings=workspace_bindings,
                        workspace_execution=workspace_execution,
                        lease_repository=database,
                        capacity=self._run_capacity,
                        owner_id=str(user_id),
                        recovery_repository=database,
                        recovery_compatibility=recovery_contracts.compatibility,
                        recovery_contract_factory=recovery_contracts.create,
                    )
                    try:
                        recovery_discovery = RecoveryDiscovery(
                            database,
                            run_service,
                            installation.installation_id,
                            lambda: token_source.valid_for(10),
                        )
                        context = AccountContext(
                            database=database,
                            web_evidence_service=web_evidence,
                            secret_store=secret_store,
                            profile_service=ProfileService(database),
                            session_service=SessionService(
                                database,
                                workspace_bindings=workspace_bindings,
                                capabilities=self._capabilities,
                            ),
                            settings_service=SettingsService(
                                database, secret_store, capabilities=self._capabilities
                            ),
                            onboarding_service=OnboardingService(database, database, secret_store),
                            chat_service=ChatService(
                                database, secret_store, self._providers, redactor=self._redactor
                            ),
                            context_preview_service=ContextPreviewService(
                                database,
                                database,
                                database,
                                secret_store,
                                available_adapters=frozenset(self._runtime_providers),
                                tool_registry=tools,
                                capabilities=self._capabilities,
                                redactor=self._redactor,
                                workspace_guidance_reader=read_workspace_guidance
                                if self._capabilities.allows("local_read")
                                else None,
                                workspace_bindings=workspace_bindings,
                                workspace_execution=workspace_execution,
                            ),
                            run_service=run_service,
                            event_hub=event_hub,
                            workspace_bindings=workspace_bindings,
                            workspace_execution=workspace_execution,
                            recovery_discovery=recovery_discovery,
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
                recovery_discovery.activate(authentication_revision=token_source.revision)
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
        await asyncio.gather(
            *(
                context.recovery_discovery.close()
                for context in tuple(self._contexts.values())
                if context.recovery_discovery is not None
            )
        )
        await self._gets_idle.wait()
        async with self._lock:
            contexts = tuple(self._contexts.values())
            self._contexts.clear()
            self._token_sources.clear()
            locks = tuple(self._locks.values())
            self._locks.clear()
        try:
            await asyncio.gather(
                *(
                    context.recovery_discovery.close()
                    for context in contexts
                    if context.recovery_discovery is not None
                )
            )
            results = await asyncio.gather(
                *(context.run_service.close() for context in contexts), return_exceptions=True
            )
        finally:
            for descriptor in locks:
                os.close(descriptor)
        for result in results:
            if isinstance(result, BaseException):
                raise result
