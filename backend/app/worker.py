import asyncio
import logging
import signal
from collections.abc import Mapping
from contextlib import suppress

import httpx

from app.application.execution import RunExecutor
from app.application.ports import StreamingProviderAdapter
from app.application.secret_redaction import SecretRedactor
from app.application.worker import WorkerPolicy, WorkerSupervisor
from app.core.config import Settings
from app.infrastructure.providers import AnthropicProvider, OpenAIProvider
from app.infrastructure.server_credentials import ServerCredentialStore
from app.infrastructure.worker_credentials import AssignedCredentialStore
from app.infrastructure.worker_repository import PostgresWorkerQueue


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    providers: Mapping[str, StreamingProviderAdapter] | None = None,
) -> None:
    if not settings.durable_execution_enabled:
        raise ValueError("Enable durable execution before starting the worker")
    if settings.worker_database_url is None:
        raise ValueError("The restricted worker database URL is required")
    if not settings.openai_api_key and not settings.anthropic_api_key:
        raise ValueError("Configure at least one provider on the worker")
    if settings.enabled_capabilities:
        raise ValueError("The durable worker supports chat only")
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed_signals.append(sig)
        except NotImplementedError, RuntimeError:
            pass
    queue = None
    try:
        queue = await PostgresWorkerQueue.open(
            settings.worker_database_url.get_secret_value(),
            max_size=min(settings.worker_capacity + 2, 16),
        )
        credentials = ServerCredentialStore(settings)
        redactor = SecretRedactor(
            *(
                key.get_secret_value().strip()
                for key in (settings.openai_api_key, settings.anthropic_api_key)
                if key is not None
            )
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)
        ) as client:
            adapters: Mapping[str, StreamingProviderAdapter] = (
                providers
                if providers is not None
                else {
                    "openai": OpenAIProvider(client),
                    "anthropic": AnthropicProvider(client),
                }
            )
            await WorkerSupervisor(
                queue,
                queue.bind,
                lambda repository: RunExecutor(
                    repository,
                    AssignedCredentialStore(queue.bind(repository.claim), credentials),
                    adapters,
                    redactor=redactor,
                ),
                policy=WorkerPolicy(capacity=settings.worker_capacity),
            ).run(stop)
    finally:
        if queue is not None:
            await queue.close()
        for sig in installed_signals:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
                if (previous := previous_signals[sig]) is not None:
                    signal.signal(sig, previous)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(Settings()))


if __name__ == "__main__":
    main()
