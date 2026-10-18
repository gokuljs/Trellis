import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.application.budgets import (
    BudgetPreset,
)
from app.application.errors import ApplicationError
from app.application.execution import RunExecutor
from app.application.ports import (
    DurableAcceptanceRepository,
    ProfileRepository,
    RunEventPublisher,
    RunLeasePort,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
    WorkspaceBindingPort,
)
from app.application.secret_redaction import COMMON_SECRET_REDACTOR, SecretRedactor
from app.application.tools import (
    ToolExecutionError,
    ToolRegistry,
)
from app.domain.capabilities import DEFAULT_RUNTIME_CAPABILITIES, RuntimeCapabilities
from app.domain.jobs import RunExecutionContext
from app.domain.models import ModelDescriptor
from app.domain.runtime import (
    ModelCallStatus,
    ModelToolCall,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
    ToolApprovalDecision,
    ToolCallRecord,
    is_terminal_run_status,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_CONCURRENT_RUNS = 4
DEFAULT_LEASE_RENEW_INTERVAL_SECONDS = 30.0
MAX_MODEL_RESPONSE_BYTES = 256_000
MAX_CONTINUATION_BYTES = 256_000


class RunService(RunExecutor):
    def __init__(
        self,
        runs: RunRepository,
        sessions: SessionRepository,
        settings: SettingsRepository,
        profiles: ProfileRepository,
        secret_store: SecretStorePort,
        providers: Mapping[str, StreamingProviderAdapter],
        event_publisher: RunEventPublisher | None = None,
        *,
        acceptance_repository: DurableAcceptanceRepository | None = None,
        durable_execution: bool = False,
        acceptance_enabled: bool = True,
        tool_registry: ToolRegistry | None = None,
        workspace_guidance_reader: Callable[[Path], Awaitable[str | None]] | None = None,
        workspace_bindings: WorkspaceBindingPort | None = None,
        lease_repository: RunLeasePort | None = None,
        lease_renew_interval_seconds: float = DEFAULT_LEASE_RENEW_INTERVAL_SECONDS,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
        capabilities: RuntimeCapabilities = DEFAULT_RUNTIME_CAPABILITIES,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("maximum concurrent runs must be positive")
        if lease_renew_interval_seconds <= 0:
            raise ValueError("lease renewal interval must be positive")
        self._durable_execution = durable_execution
        self._acceptance_enabled = acceptance_enabled
        self._runs = runs
        self._acceptance = acceptance_repository
        self._sessions = sessions
        self._settings = settings
        self._profiles = profiles
        self._secret_store = secret_store
        self._providers = providers
        self._event_publisher = event_publisher
        self._tool_registry = tool_registry
        self._capabilities = capabilities
        self._redactor = redactor or COMMON_SECRET_REDACTOR
        self._workspace_guidance_reader = workspace_guidance_reader
        self._workspace_bindings = workspace_bindings
        self._lease_repository = lease_repository
        self._lease_renew_interval_seconds = lease_renew_interval_seconds
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self._admission_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_finalizers: dict[str, asyncio.Task[None]] = {}
        self._lease_monitors: dict[str, asyncio.Task[None]] = {}
        self._lease_lost: set[str] = set()
        self._reschedule_pending: set[str] = set()
        self._closing = False

    async def create_run(
        self,
        session_id: str,
        client_request_id: str,
        content: str,
        *,
        turn_id: str | None = None,
        budget_preset: BudgetPreset | None = None,
        model_id: str | None = None,
    ) -> RunSnapshot:
        if self._durable_execution:
            return await self.accept_run(
                session_id,
                client_request_id,
                content,
                turn_id=turn_id,
                budget_preset=budget_preset,
                model_id=model_id,
            )
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        normalized_content = self._redactor.redact_text(content.strip())
        if not normalized_content:
            raise ApplicationError("message_empty", "Message content cannot be empty.")

        models = await self._settings.list_models()
        selected_model_id = (
            model_id
            if model_id is not None
            else session.model_id or await self._settings.get_selected_model_id()
        )
        model = next((item for item in models if item.id == selected_model_id), None)
        if model is None:
            raise ApplicationError("model_not_available", "The selected model is unavailable.")
        selected_budget = (
            budget_preset
            if budget_preset is not None
            else await self._settings.get_default_budget_preset()
        )
        if selected_budget not in {"conservative", "longer"}:
            raise ApplicationError("invalid_budget_preset", "Choose an available run budget.")
        self._require_provider(model)
        api_key = await self._secret_store.get(model.provider_id)
        if model.requires_api_key and api_key is None:
            raise ApplicationError(
                "provider_not_configured",
                "The selected provider is not configured on this server.",
            )

        async with self._admission_lock:
            if self._closing:
                raise ApplicationError("runtime_shutting_down", "The runtime is shutting down.")
            try:
                run = await self._runs.create_run(
                    session.id,
                    turn_id or client_request_id,
                    client_request_id,
                    normalized_content,
                    model,
                    budget_preset=selected_budget,
                )
            except ValueError as error:
                message = str(error)
                if "active run already exists" in message:
                    raise ApplicationError(
                        "run_in_progress",
                        "Another run is already in progress for this session.",
                    ) from None
                if "conflicts with a previous payload" in message:
                    raise ApplicationError(
                        "run_conflict",
                        "This request ID is already associated with different run input.",
                    ) from None
                if "session not found" in message:
                    raise ApplicationError("session_not_found", "Session not found.") from None
                raise ApplicationError(
                    "run_invalid", "The run request could not be accepted."
                ) from None

            if run.status is RunStatus.QUEUED and self._can_execute(run.id):
                self._schedule(run.id)
            return run

    async def lookup_run(self, session_id: str, client_request_id: str) -> RunSnapshot | None:
        if self._acceptance is None:
            raise ApplicationError("runtime_not_configured", "Durable execution is unavailable.")
        try:
            return await self._acceptance.lookup_run(session_id, client_request_id)
        except ValueError as error:
            raise self._acceptance_error(error) from None

    async def accept_run(
        self,
        session_id: str,
        client_request_id: str,
        content: str,
        *,
        turn_id: str | None = None,
        budget_preset: BudgetPreset | None = None,
        model_id: str | None = None,
    ) -> RunSnapshot:
        if self._acceptance is None:
            raise ApplicationError("runtime_not_configured", "Durable execution is unavailable.")
        normalized = content.strip()
        if not normalized:
            raise ApplicationError("message_empty", "Message content cannot be empty.")
        if len(content) > 100_000 or not 1 <= len(client_request_id) <= 200:
            raise ApplicationError("run_invalid", "The run request is invalid.")
        if turn_id is not None and not 1 <= len(turn_id) <= 200:
            raise ApplicationError("run_invalid", "The run request is invalid.")
        submission: dict[str, object] = {
            "content": normalized,
            "turnId": turn_id or client_request_id,
        }
        if model_id is not None:
            submission["modelId"] = model_id
        if budget_preset is not None:
            submission["budgetPreset"] = budget_preset
        try:
            existing = await self._acceptance.match_run_request(
                session_id, client_request_id, submission
            )
            if existing is not None:
                return existing
            if self._durable_execution and (not self._acceptance_enabled or self._closing):
                raise ApplicationError(
                    "runtime_unavailable",
                    "New requests are temporarily paused. Saved work is safe.",
                )
            session = await self._sessions.get_session(session_id)
            if session is None:
                raise ApplicationError("session_not_found", "Session not found.")
            models = await self._settings.list_models()
            selected_model_id = (
                model_id
                if model_id is not None
                else (session.model_id or await self._settings.get_selected_model_id())
            )
            model = next((item for item in models if item.id == selected_model_id), None)
            if model is None:
                raise ApplicationError("model_not_available", "The selected model is unavailable.")
            selected_budget = (
                budget_preset
                if budget_preset is not None
                else (await self._settings.get_default_budget_preset())
            )
            if selected_budget not in {"conservative", "longer"}:
                raise ApplicationError("invalid_budget_preset", "Choose an available run budget.")
            self._require_provider(model)
            if model.requires_api_key and await self._secret_store.get(model.provider_id) is None:
                raise ApplicationError(
                    "provider_not_configured",
                    "The selected provider is not configured on this server.",
                )
            return await self._acceptance.accept_run(
                session_id,
                client_request_id,
                self._redactor.redact_text(normalized),
                model,
                submission,
                budget_preset=selected_budget,
            )
        except ValueError as error:
            raise self._acceptance_error(error) from None

    @staticmethod
    def _acceptance_error(error: ValueError) -> ApplicationError:
        message = str(error)
        if "conflicts with a previous payload" in message:
            return ApplicationError("run_conflict", "This request ID has different run input.")
        if "session not found" in message:
            return ApplicationError("session_not_found", "Session not found.")
        if "active run already exists" in message:
            return ApplicationError("run_in_progress", "Another run is in progress for this chat.")
        if "account run capacity reached" in message:
            return ApplicationError(
                "run_capacity", "Your account has 16 accepted requests. Wait for one to finish."
            )
        return ApplicationError("run_invalid", "The run request could not be accepted.")

    def _can_execute(self, run_id: str) -> bool:
        return (
            not self._durable_execution
            and run_id not in self._lease_lost
            and (self._lease_repository is None or self._lease_repository.owns_run_lease(run_id))
        )

    async def wait_for_run(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await task
            finally:
                finalizer = self._cancel_finalizers.get(run_id)
                if finalizer is not None:
                    await finalizer
        else:
            finalizer = self._cancel_finalizers.get(run_id)
            if finalizer is not None:
                await finalizer

    async def get_run(self, run_id: str) -> RunSnapshot | None:
        return await self._recover_if_expired(await self._runs.get_run(run_id))

    async def _recover_if_expired(self, run: RunSnapshot | None) -> RunSnapshot | None:
        if (
            self._durable_execution
            or self._lease_repository is None
            or run is None
            or is_terminal_run_status(run.status)
        ):
            return run
        if run.lease_expires_at is None:
            return run
        try:
            expiry = datetime.fromisoformat(run.lease_expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeError("Cloud run has an invalid lease expiry") from None
        if expiry.tzinfo is None:
            raise RuntimeError("Cloud run has an invalid lease expiry")
        if expiry > datetime.now(UTC):
            return run
        return await self._lease_repository.recover_expired_run(run.id)

    async def _require_local_control(self, run_id: str) -> None:
        if self._lease_repository is None or self._can_execute(run_id):
            return
        if await self._runs.get_run(run_id) is None:
            raise ApplicationError("run_not_found", "Run not found.")
        raise ApplicationError("run_not_owned", "This run is active on another computer.")

    async def get_latest_run_for_session(self, session_id: str) -> RunSnapshot | None:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        return await self._recover_if_expired(
            await self._runs.get_latest_run_for_session(session_id)
        )

    async def list_session_runs(
        self,
        session_id: str,
        offset: int,
        limit: int,
        *,
        turn_id: str | None = None,
        newest_first: bool = False,
    ) -> tuple[list[RunSnapshot], int | None]:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        runs = await self._runs.list_runs_for_session(
            session_id, offset, limit + 1, turn_id=turn_id, newest_first=newest_first
        )
        visible = [await self._recover_if_expired(run) for run in runs[:limit]]
        return [run for run in visible if run is not None], offset + limit if len(
            runs
        ) > limit else None

    async def list_session_run_events(
        self, session_id: str, run_id: str, after_sequence: int, limit: int
    ) -> tuple[list[RunEvent], int | None]:
        if await self._sessions.get_session(session_id) is None:
            raise ApplicationError("session_not_found", "Session not found.")
        run = await self._runs.get_run(run_id)
        if run is None or run.session_id != session_id:
            raise ApplicationError("run_not_found", "Run not found.")
        events = await self._runs.list_run_events(run_id, after_sequence, limit=limit + 1)
        page = events[:limit]
        next_sequence = page[-1].sequence if len(events) > limit else None
        return page, next_sequence

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int,
        *,
        limit: int = 500,
    ) -> list[RunEvent]:
        return await self._runs.list_run_events(run_id, after_sequence, limit=limit)

    async def cancel_run(self, run_id: str) -> RunSnapshot:
        if not self._durable_execution:
            await self._require_local_control(run_id)
        try:
            run, event = await self._runs.request_run_cancellation(run_id)
        except ValueError as error:
            if str(error) == "run not found":
                raise ApplicationError("run_not_found", "Run not found.") from None
            raise
        if self._durable_execution:
            if event is not None:
                await self._publish(event)
            return run
        task = self._tasks.get(run_id)
        if (
            event is not None
            and run.status in {RunStatus.CANCELLING, RunStatus.CANCELLED}
            and task is not None
            and not task.done()
        ):
            task.cancel()
        if event is not None:
            await self._publish(event)
        if event is not None and run.status is RunStatus.CANCELLING:
            current = await self._runs.get_run(run_id)
            if (
                current is not None
                and current.status is RunStatus.CANCELLING
                and task is not None
                and not task.done()
            ):
                self._schedule_cancel_finalizer(run_id, task)
            elif current is not None and current.status is RunStatus.CANCELLING:
                calls = await self._runs.list_model_calls(run_id)
                active_call = next(
                    (
                        call
                        for call in reversed(calls)
                        if call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}
                    ),
                    None,
                )
                await self._mark_cancelled(run_id, active_call)
                persisted = await self._runs.get_run(run_id)
                if persisted is not None:
                    run = persisted
        return run

    def _schedule_cancel_finalizer(self, run_id: str, task: asyncio.Task[None]) -> None:
        existing = self._cancel_finalizers.get(run_id)
        if existing is not None and not existing.done():
            return
        finalizer = asyncio.create_task(
            self._finalize_cancelled_task(run_id, task), name=f"trellis-cancel-{run_id}"
        )
        self._cancel_finalizers[run_id] = finalizer
        finalizer.add_done_callback(lambda done: self._discard_cancel_finalizer(run_id, done))

    async def _finalize_cancelled_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        await asyncio.gather(task, return_exceptions=True)
        if not self._can_execute(run_id):
            return
        current = await self._runs.get_run(run_id)
        if current is not None and current.status is RunStatus.CANCELLING:
            await self._mark_cancelled(run_id, None)

    def _discard_cancel_finalizer(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._cancel_finalizers.get(run_id) is task:
            del self._cancel_finalizers[run_id]

    async def respond_to_tool_approval(
        self,
        run_id: str,
        tool_call_id: str,
        decision: ToolApprovalDecision,
    ) -> tuple[RunSnapshot, ToolCallRecord]:
        if self._closing:
            raise ApplicationError("runtime_shutting_down", "The runtime is shutting down.")
        if not self._durable_execution:
            await self._require_local_control(run_id)
        stored_call = next(
            (call for call in await self._runs.list_tool_calls(run_id) if call.id == tool_call_id),
            None,
        )
        if stored_call is not None and not self._durable_execution:
            if self._tool_registry is None:
                raise ApplicationError(
                    "tool_execution_unavailable", "Tool execution is unavailable."
                )
            try:
                self._tool_registry.check_capabilities(
                    ModelToolCall(
                        stored_call.provider_call_id, stored_call.name, stored_call.arguments
                    ),
                    capabilities=self._capabilities,
                )
            except ToolExecutionError as error:
                raise ApplicationError(error.code, error.message) from None
        try:
            tool_call, event = await self._runs.record_tool_approval_decision(
                run_id, tool_call_id, decision
            )
        except ValueError as error:
            message = str(error)
            if message == "run not found":
                raise ApplicationError("run_not_found", "Run not found.") from None
            if message == "tool call not found for run":
                raise ApplicationError("tool_call_not_found", "Tool call not found.") from None
            if message == "tool approval decision already differs":
                raise ApplicationError(
                    "approval_conflict", "This tool approval was already answered differently."
                ) from None
            raise ApplicationError(
                "approval_not_pending", "This tool call is not waiting for approval."
            ) from None
        if event is not None:
            await self._publish(event)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise ApplicationError("run_not_found", "Run not found.")
        if not self._durable_execution and run.status is RunStatus.WAITING_FOR_APPROVAL:
            self._schedule(run.id)
        return run, tool_call

    async def resume_decided_approvals(self) -> None:
        if self._durable_execution:
            return
        for run_id in await self._runs.list_decided_approval_runs():
            self._schedule(run_id)

    async def close(self) -> None:
        async with self._admission_lock:
            self._closing = True
            pending = tuple(self._tasks.items())
        tasks = tuple(task for _run_id, task in pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        finalizers = tuple(self._cancel_finalizers.values())
        if finalizers:
            await asyncio.gather(*finalizers, return_exceptions=True)
        for run_id, _task in pending:
            if self._can_execute(run_id):
                run = await self._runs.get_run(run_id)
                if run is not None and run.status is RunStatus.QUEUED:
                    await self._interrupt_queued_run(run_id)
        monitors = tuple(self._lease_monitors.values())
        for monitor in monitors:
            monitor.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)

    def _schedule(self, run_id: str) -> None:
        if not self._can_execute(run_id):
            return
        self._start_lease_monitor(run_id)
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            self._reschedule_pending.add(run_id)
            return
        task = asyncio.create_task(self._run_with_limit(run_id), name=f"trellis-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._discard_task(run_id, done))

    def _start_lease_monitor(self, run_id: str) -> None:
        if self._lease_repository is None:
            return
        existing = self._lease_monitors.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._monitor_lease(run_id), name=f"trellis-lease-{run_id}")
        self._lease_monitors[run_id] = task
        task.add_done_callback(lambda done: self._discard_lease_monitor(run_id, done))

    def _discard_lease_monitor(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._lease_monitors.get(run_id) is task:
            del self._lease_monitors[run_id]

    async def _monitor_lease(self, run_id: str) -> None:
        assert self._lease_repository is not None
        while True:
            await asyncio.sleep(self._lease_renew_interval_seconds)
            try:
                run = await self._lease_repository.renew_run_lease(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Stopping local execution after lease renewal failed for %s (%s)",
                    run_id,
                    type(error).__name__,
                )
                self._lease_lost.add(run_id)
                active = self._tasks.get(run_id)
                if active is not None and not active.done():
                    active.cancel()
                return
            if is_terminal_run_status(run.status):
                return

    def _discard_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]
        if run_id in self._reschedule_pending:
            self._reschedule_pending.discard(run_id)
            if not self._closing:
                self._schedule(run_id)

    async def _run_with_limit(self, run_id: str) -> None:
        try:
            async with self._slots:
                await self._execute(run_id)
        except asyncio.CancelledError:
            if run_id in self._lease_lost:
                raise
            run = await self._runs.get_run(run_id)
            if run is not None and run.status is RunStatus.QUEUED:
                await self._interrupt_queued_run(run_id)
            elif run is not None and run.status is RunStatus.CANCELLING:
                await self._mark_cancelled(run_id, None)
            raise
        except Exception as error:
            logger.error(
                "Unexpected failure before run execution completed for %s (error type: %s)",
                run_id,
                type(error).__name__,
            )
            if not self._can_execute(run_id):
                return
            try:
                run = await self._runs.get_run(run_id)
                if run is not None and run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    event = await self._runs.transition_run_record(
                        run_id,
                        RunStatus.FAILED,
                        RunEventType.FAILED,
                        {
                            "code": "runtime_internal_error",
                            "message": "Trellis could not start this run.",
                        },
                        error_code="runtime_internal_error",
                        error_message="Trellis could not start this run.",
                        stop_reason="runtime_internal_error",
                    )
                    await self._publish(event)
            except Exception as persist_error:
                logger.error(
                    "Could not persist failure for run %s (error type: %s)",
                    run_id,
                    type(persist_error).__name__,
                )

    async def _interrupt_queued_run(self, run_id: str) -> None:
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.INTERRUPTED,
            RunEventType.INTERRUPTED,
            {
                "code": "runtime_shutdown",
                "message": "The run was interrupted during shutdown.",
            },
            error_code="runtime_shutdown",
            error_message="The run was interrupted during shutdown.",
            stop_reason="runtime_shutdown",
        )
        await self._publish(event)

    async def _execute(self, run_id: str) -> None:
        run = await self._runs.get_run(run_id)
        if run is None or run.status not in {RunStatus.QUEUED, RunStatus.WAITING_FOR_APPROVAL}:
            return
        try:
            if run.status is RunStatus.QUEUED:
                event = await self._runs.transition_run_record(
                    run_id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
                )
                await self._publish(event)
                run = await self._runs.get_run(run_id)
                assert run is not None
            else:
                run, event = await self._runs.resume_approved_run(run_id)
                await self._publish(event)
            model = await self._get_run_model(run)
            profile = await self._profiles.get_profile()
            session = await self._sessions.get_session(run.session_id)
            if session is None:
                raise ApplicationError("session_not_found", "Session not found.")
            await self.execute(RunExecutionContext(profile.id, run, session, model))
        except asyncio.CancelledError:
            if self._can_execute(run_id):
                current = await self._runs.get_run(run_id)
                if current is not None and current.status is RunStatus.CANCELLING:
                    await self._mark_cancelled(run_id, None)
                elif current is not None and current.status is RunStatus.RUNNING:
                    await self._mark_interrupted(run_id, None)
            raise
        except ApplicationError as error:
            if self._can_execute(run_id):
                await self._fail_or_cancel(run_id, None, error.code, error.message)

    async def _get_run_model(self, run: RunSnapshot) -> ModelDescriptor:
        models = await self._settings.list_models()
        model = next((item for item in models if item.id == run.model_id), None)
        if model is None or (
            model.provider_id,
            model.adapter_kind,
            model.upstream_model_id,
        ) != (run.provider_id, run.adapter_kind, run.upstream_model_id):
            raise ApplicationError("model_not_available", "The run's model is unavailable.")
        return model
