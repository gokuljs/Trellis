import asyncio
from dataclasses import replace

import pytest
from run_fixtures import MODEL
from test_compaction_recovery import FileLedger, Provider, context, intent_for, native_plan
from test_recovery_contracts import CONTRACT

from app.application.errors import ApplicationError
from app.domain.runtime import ModelCallStatus, ModelStreamEvent, RunEventType


@pytest.mark.parametrize("purpose", ["native", "summary"])
@pytest.mark.parametrize("stop", ["cancellation", "lease_transfer"])
def test_compaction_checks_ownership_after_publishing_dispatch_phase(tmp_path, purpose, stop):
    async def scenario():
        ledger = FileLedger(tmp_path / "ledger.json")
        ledger.run = replace(ledger.run, recovery_contract=replace(CONTRACT, workspace=None))
        runtime, provider = context(ledger), Provider()
        intent = intent_for(ledger)
        owns_dispatch = True

        async def publish(event):
            nonlocal owns_dispatch
            if event.event_type is RunEventType.PHASE:
                owns_dispatch = False

        async def guard(run):
            assert run.id == ledger.run.id
            if not owns_dispatch:
                if stop == "cancellation":
                    raise asyncio.CancelledError
                raise ApplicationError("run_lease_lost", "Ownership changed during publication")

        runtime.publish, runtime.before_dispatch = publish, guard
        with pytest.raises((ApplicationError, asyncio.CancelledError)):
            if purpose == "native":
                await runtime._compact(
                    ledger.run,
                    MODEL,
                    provider,
                    native_plan(intent),
                    "key",
                    "user",
                    1,
                    intent=intent,
                )
            else:
                await runtime._summarize(
                    ledger.run,
                    MODEL,
                    provider,
                    intent.summary_plan,
                    "key",
                    "user",
                    1,
                    intent=intent,
                )
        assert (provider.native_calls, provider.summary_calls) == (0, 0)
        assert ledger.calls[0].status is ModelCallStatus.STREAMING
        assert ledger.checkpoint is None

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["cancellation", "connection_lost"])
def test_incomplete_summary_preserves_the_dispatched_ledger_for_unknown_usage_stop(tmp_path, stop):
    class InterruptedSummary(Provider):
        async def stream(self, request, api_key, user_id):
            yield ModelStreamEvent(
                "usage",
                input_tokens=100,
                output_tokens=10,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
            if stop == "cancellation":
                raise asyncio.CancelledError
            raise ApplicationError("provider_upstream_failed", "The stream was lost")

    async def scenario():
        ledger = FileLedger(tmp_path / "ledger.json")
        ledger.run = replace(ledger.run, recovery_contract=replace(CONTRACT, workspace=None))
        runtime = context(ledger)
        intent = intent_for(ledger)
        with pytest.raises((ApplicationError, asyncio.CancelledError)):
            await runtime._summarize(
                ledger.run,
                MODEL,
                InterruptedSummary(),
                intent.summary_plan,
                "key",
                "user",
                1,
                intent=intent,
            )
        saved = FileLedger(ledger.path).calls[0]
        assert saved.status is ModelCallStatus.STREAMING
        assert saved.input_tokens == 100 and saved.output_tokens == 10
        assert ledger.checkpoint is None

    asyncio.run(scenario())
