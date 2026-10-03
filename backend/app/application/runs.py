import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

from app.application.errors import ApplicationError, ProviderError
from app.application.ports import (
    ProfileRepository,
    RunEventPublisher,
    RunRepository,
    SecretStorePort,
    SessionRepository,
    SettingsRepository,
    StreamingProviderAdapter,
)
from app.domain.models import ModelDescriptor
from app.domain.runtime import (
    ModelCallRecord,
    ModelCallStatus,
    ModelMessage,
    ModelRequest,
    RunEvent,
    RunEventType,
    RunSnapshot,
    RunStatus,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_CONCURRENT_RUNS = 4


class RunService:
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
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("maximum concurrent runs must be positive")
        self._runs = runs
        self._sessions = sessions
        self._settings = settings
        self._profiles = profiles
        self._secret_store = secret_store
        self._providers = providers
        self._event_publisher = event_publisher
        self._slots = asyncio.Semaphore(max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def create_run(
        self,
        session_id: str,
        client_request_id: str,
        content: str,
        *,
        turn_id: str | None = None,
    ) -> RunSnapshot:
        if self._closing:
            raise ApplicationError("runtime_shutting_down", "The runtime is shutting down.")
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        normalized_content = content.strip()
        if not normalized_content:
            raise ApplicationError("message_empty", "Message content cannot be empty.")

        models = await self._settings.list_models()
        selected_model_id = await self._settings.get_selected_model_id()
        model = next((item for item in models if item.id == selected_model_id), None)
        if model is None:
            raise ApplicationError("model_not_available", "The selected model is unavailable.")
        self._require_provider(model)
        api_key = await self._secret_store.get(model.provider_id)
        if model.requires_api_key and api_key is None:
            raise ApplicationError(
                "provider_not_configured",
                "Add an API key for the selected provider in Settings.",
            )

        try:
            run = await self._runs.create_run(
                session.id,
                turn_id or client_request_id,
                client_request_id,
                normalized_content,
                model,
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

        if run.status is RunStatus.QUEUED:
            self._schedule(run.id)
        return run

    async def wait_for_run(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await task

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, run_id: str) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_with_limit(run_id), name=f"trellis-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda done: self._discard_task(run_id, done))

    def _discard_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]

    async def _run_with_limit(self, run_id: str) -> None:
        async with self._slots:
            await self._execute(run_id)

    async def _execute(self, run_id: str) -> None:
        run = await self._runs.get_run(run_id)
        if run is None or run.status is not RunStatus.QUEUED:
            return

        call: ModelCallRecord | None = None
        started = False
        try:
            started_event = await self._runs.transition_run_record(
                run_id,
                RunStatus.RUNNING,
                RunEventType.STARTED,
                {"status": RunStatus.RUNNING.value},
            )
            started = True
            await self._publish(started_event)

            model = await self._get_run_model(run)
            provider = self._require_provider(model)
            api_key = await self._secret_store.get(run.provider_id)
            if model.requires_api_key and api_key is None:
                raise ApplicationError(
                    "provider_not_configured",
                    "Add an API key for the selected provider in Settings.",
                )
            profile = await self._profiles.get_profile()
            messages = await self._sessions.list_messages(run.session_id)
            model_messages = tuple(
                ModelMessage(role=message.role, content=message.content) for message in messages
            )
            request = ModelRequest(
                provider_id=run.provider_id,
                model_id=run.model_id,
                adapter_kind=run.adapter_kind,
                upstream_model_id=run.upstream_model_id,
                messages=model_messages,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            request_snapshot: dict[str, object] = {
                "provider_id": request.provider_id,
                "model_id": request.model_id,
                "adapter_kind": request.adapter_kind,
                "upstream_model_id": request.upstream_model_id,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                "max_output_tokens": request.max_output_tokens,
            }
            call = await self._runs.create_model_call(
                run.id,
                1,
                model,
                request_snapshot,
            )
            call = await self._runs.update_model_call(call.id, ModelCallStatus.STREAMING)

            output_parts: list[str] = []
            usage: dict[str, int | None] = {
                "input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "cached_tokens": None,
            }
            provider_response_id: str | None = None
            finish_reason: str | None = None
            deadline = datetime.fromisoformat(run.deadline_at.replace("Z", "+00:00"))
            timeout_seconds = (deadline - datetime.now(UTC)).total_seconds()
            if timeout_seconds <= 0:
                raise ProviderError("provider_timeout", "The run deadline was reached.")

            try:
                async with asyncio.timeout(timeout_seconds):
                    async for event in provider.stream(
                        request,
                        api_key or "",
                        profile.id,
                    ):
                        if event.kind == "text_delta":
                            if event.text is None:
                                raise ProviderError(
                                    "provider_invalid_response",
                                    "The provider returned an invalid text event.",
                                )
                            output_parts.append(event.text)
                            await self._append_and_publish(
                                run.id,
                                RunEventType.ASSISTANT_DELTA,
                                {"text": event.text},
                            )
                        elif event.kind == "usage":
                            usage = {
                                "input_tokens": event.input_tokens,
                                "output_tokens": event.output_tokens,
                                "reasoning_tokens": event.reasoning_tokens,
                                "cached_tokens": event.cached_tokens,
                            }
                            provider_response_id = (
                                event.provider_response_id or provider_response_id
                            )
                            call = await self._runs.update_model_call(
                                call.id,
                                ModelCallStatus.STREAMING,
                                provider_response_id=provider_response_id,
                                input_tokens=usage["input_tokens"],
                                output_tokens=usage["output_tokens"],
                                reasoning_tokens=usage["reasoning_tokens"],
                                cached_tokens=usage["cached_tokens"],
                            )
                            await self._append_and_publish(
                                run.id,
                                RunEventType.MODEL_USAGE,
                                {
                                    **{
                                        key: value
                                        for key, value in usage.items()
                                        if value is not None
                                    },
                                    **(
                                        {"provider_response_id": provider_response_id}
                                        if provider_response_id is not None
                                        else {}
                                    ),
                                },
                            )
                        elif event.kind == "completed":
                            provider_response_id = (
                                event.provider_response_id or provider_response_id
                            )
                            finish_reason = event.finish_reason
            except TimeoutError:
                raise ProviderError("provider_timeout", "The run deadline was reached.") from None

            assistant_content = "".join(output_parts).strip()
            if not assistant_content:
                raise ProviderError(
                    "provider_invalid_response",
                    "The provider returned an empty response.",
                )
            call = await self._runs.update_model_call(
                call.id,
                ModelCallStatus.COMPLETED,
                response_snapshot={"finish_reason": finish_reason},
                provider_response_id=provider_response_id,
                finish_reason=finish_reason,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                cached_tokens=usage["cached_tokens"],
            )
            await self._append_and_publish(
                run.id,
                RunEventType.MODEL_COMPLETED,
                {
                    "model_call_id": call.id,
                    "provider_response_id": provider_response_id,
                    "finish_reason": finish_reason,
                },
            )
            _message, completion_events = await self._runs.complete_run(run.id, assistant_content)
            for event in completion_events:
                await self._publish(event)
        except asyncio.CancelledError:
            if started:
                await self._mark_interrupted(run_id, call)
            raise
        except ProviderError as error:
            if started:
                await self._fail(run_id, call, error.code, error.message)
        except ApplicationError as error:
            if started:
                await self._fail(run_id, call, error.code, error.message)
        except Exception:
            logger.exception("Unexpected failure while executing run %s", run_id)
            if started:
                await self._fail(
                    run_id,
                    call,
                    "runtime_internal_error",
                    "Trellis could not complete this run.",
                )

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

    def _require_provider(self, model: ModelDescriptor) -> StreamingProviderAdapter:
        if not model.supports_streaming:
            raise ApplicationError("model_not_streaming", "The selected model cannot stream.")
        provider = self._providers.get(model.adapter_kind)
        if provider is None:
            raise ApplicationError("provider_not_available", "The model adapter is not available.")
        return provider

    async def _append_and_publish(
        self,
        run_id: str,
        event_type: RunEventType,
        data: dict[str, object],
    ) -> RunEvent:
        event = await self._runs.append_run_event(run_id, event_type, data)
        await self._publish(event)
        return event

    async def _publish(self, event: RunEvent) -> None:
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher.publish(event)
        except Exception:
            logger.exception(
                "Could not publish persisted event %s for run %s", event.sequence, event.run_id
            )

    async def _fail(
        self,
        run_id: str,
        call: ModelCallRecord | None,
        error_code: str,
        error_message: str,
    ) -> None:
        if call is not None and call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            await self._runs.update_model_call(
                call.id,
                ModelCallStatus.FAILED,
                error_code=error_code,
                error_message=error_message,
            )
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.FAILED,
            RunEventType.FAILED,
            {"code": error_code, "message": error_message},
            error_code=error_code,
            error_message=error_message,
            stop_reason=error_code,
        )
        await self._publish(event)

    async def _mark_interrupted(self, run_id: str, call: ModelCallRecord | None) -> None:
        if call is not None and call.status in {ModelCallStatus.PENDING, ModelCallStatus.STREAMING}:
            await self._runs.update_model_call(call.id, ModelCallStatus.CANCELLED)
        event = await self._runs.transition_run_record(
            run_id,
            RunStatus.INTERRUPTED,
            RunEventType.INTERRUPTED,
            {"code": "runtime_shutdown", "message": "The run was interrupted during shutdown."},
            error_code="runtime_shutdown",
            error_message="The run was interrupted during shutdown.",
            stop_reason="runtime_shutdown",
        )
        await self._publish(event)
