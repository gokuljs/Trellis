import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from run_fixtures import Provider, Repository, plan, service

from app.application.run_context import RunContext
from app.domain.runtime import RunEventType, RunStatus


@pytest.mark.parametrize("change", ["cancel", "generation", "inactive_status"])
def test_generation_rechecks_ownership_and_cancellation_after_phase_publication(
    monkeypatch, change
):
    repository = Repository()

    class CountingProvider(Provider):
        calls = 0

        async def stream(self, *arguments):
            self.calls += 1
            async for event in super().stream(*arguments):
                yield event

    class Publisher:
        async def publish(self, event):
            if event.event_type is RunEventType.PHASE and event.data.get("phase") == "answering":
                await asyncio.sleep(0)
                if change == "cancel":
                    repository.run = replace(
                        repository.run, status=RunStatus.CANCELLING, cancel_requested_at="now"
                    )
                elif change == "generation":
                    repository.run = replace(
                        repository.run, generation=repository.run.generation + 1
                    )
                else:
                    repository.run = replace(repository.run, status=RunStatus.WAITING_FOR_APPROVAL)

    provider = CountingProvider()
    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))

    async def exercise():
        runtime = service(repository, provider)
        runtime._event_publisher = Publisher()
        admitted = await runtime.create_run("session", "request", "Inspect this")
        await runtime.wait_for_run(admitted.id)
        assert provider.calls == 0
        if change == "cancel":
            assert repository.run.status is RunStatus.CANCELLED
        await runtime.close()

    asyncio.run(exercise())


def test_generation_does_not_dispatch_after_ownership_check_uses_up_deadline(monkeypatch):
    repository = Repository()
    dispatched = []
    expired = False

    class CountingProvider(Provider):
        async def stream(self, *arguments):
            dispatched.append(True)
            async for event in super().stream(*arguments):
                yield event

    async def slow_guard(self, run):
        nonlocal expired
        await asyncio.sleep(0)
        expired = True

    monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
    monkeypatch.setattr("app.application.runs.RunService._guard_model_dispatch", slow_guard)
    monkeypatch.setattr(
        "app.application.runs.RunService._remaining_seconds",
        staticmethod(lambda run: 0 if expired else 10),
    )

    async def exercise():
        runtime = service(repository, CountingProvider())
        admitted = await runtime.create_run("session", "request", "Inspect this")
        await runtime.wait_for_run(admitted.id)
        assert not dispatched
        assert repository.run.status is RunStatus.FAILED
        assert repository.calls[0].error_code == "provider_timeout"
        await runtime.close()

    asyncio.run(exercise())


def test_unknown_model_usage_never_becomes_a_complete_budget_total():
    from run_fixtures import MODEL, model_record

    from app.application.errors import ProviderError
    from app.application.runs import RunService
    from app.domain.runtime import ModelCallStatus

    unknown = replace(
        model_record(),
        status=ModelCallStatus.CANCELLED,
        input_tokens=100,
        output_tokens=10,
        cached_tokens=0,
        cache_creation_tokens=0,
        accounted_total_tokens=None,
        estimated_cost=None,
        error_code="model_outcome_unknown",
    )
    with pytest.raises(ProviderError) as failure:
        RunService._budget_totals(MODEL.id, [unknown])
    assert failure.value.code == "provider_usage_unavailable"
