import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from run_fixtures import MODEL, Provider, Repository, plan, run_record, service

from app.application.compaction import build_summary_request
from app.application.ports import RunRepository, StreamingProviderAdapter
from app.application.run_context import CompactionIntent, RunContext
from app.application.secret_redaction import SecretRedactor
from app.core.config import Settings
from app.domain.context import ContextItem, NativeCompactionResult
from app.domain.runtime import ModelCallStatus, ModelStreamEvent
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

USER_ID = UUID("16000000-0000-0000-0000-000000000001")


def repository(client: httpx.AsyncClient) -> SupabaseRepository:
    source = VerifiedTokenSource(USER_ID)
    source.record_verified("user-token", USER_ID, datetime.now(UTC) + timedelta(hours=1))
    return SupabaseRepository(
        Settings(
            _env_file=None,
            supabase_url="https://test.supabase.co",
            supabase_publishable_key="sb_publishable_test",
        ),
        USER_ID,
        source,
        client,
    )


def test_model_call_update_sends_normalized_usage_and_explicit_unknown_clearing() -> None:
    requests = []
    row = {
        "id": "model-call-id",
        "user_id": str(USER_ID),
        "run_id": "run-id",
        "step_index": 1,
        "provider_id": "openai",
        "model_id": "openai:gpt-5.5",
        "adapter_kind": "openai",
        "status": "streaming",
        "request_snapshot": {},
        "response_snapshot": None,
        "started_at": "2026-10-16T00:00:01+00:00",
        "accounted_total_tokens": 42,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=row)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repo = repository(client)
            repo._model_call_runs["model-call-id"] = "run-id"
            repo._capabilities["run-id"] = "a" * 32
            repo._lease_expires["run-id"] = datetime.now(UTC) + timedelta(minutes=5)
            call = await repo.update_model_call(
                "model-call-id",
                ModelCallStatus.STREAMING,
                input_tokens=40,
                output_tokens=2,
                estimated_cost=0.1,
                accounted_total_tokens=42,
            )
            assert call.accounted_total_tokens == 42
            await repo.update_model_call(
                "model-call-id",
                ModelCallStatus.STREAMING,
                clear_estimated_cost=True,
                clear_accounted_total_tokens=True,
            )

    asyncio.run(exercise())
    first = json.loads(requests[0].content)["p_payload"]
    second = json.loads(requests[1].content)["p_payload"]
    assert (first["accounted_total_tokens"], first["estimated_cost"]) == (42, 0.1)
    assert first["clear_accounted_total_tokens"] is False
    assert second["clear_accounted_total_tokens"] and second["clear_estimated_cost"]


@pytest.mark.parametrize("purpose", ["answer", "summary", "native_compaction"])
def test_billable_paths_persist_normalized_usage(purpose, monkeypatch) -> None:
    repo = Repository()
    provider = Provider()

    async def exercise() -> None:
        if purpose == "answer":
            monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
            runtime = service(repo, provider)
            admitted = await runtime.create_run("session", "request", "test input")
            await runtime.wait_for_run(admitted.id)
            await runtime.close()
        else:
            context = RunContext(
                cast(RunRepository, repo),
                Mock(),
                Mock(),
                lambda _id, _calls: (0, 0),
                AsyncMock(),
                SecretRedactor(),
            )
            if purpose == "native_compaction":
                native = Mock()
                native.compact = AsyncMock(
                    return_value=NativeCompactionResult(
                        "openai_window",
                        None,
                        (
                            ModelStreamEvent(
                                "usage",
                                input_tokens=100,
                                output_tokens=10,
                                cached_tokens=0,
                                cache_creation_tokens=0,
                            ),
                        ),
                        None,
                        "native_context_invalid",
                    )
                )
                await context._compact(run_record(), MODEL, native, plan(), "key", "user", 1)
            else:
                source = ContextItem(str(USER_ID), "history", plan().request.messages[0])
                generation = replace(plan(), sources=(source,), input_hash="a" * 64)
                summary = replace(
                    plan(), request=build_summary_request(generation.request, (source,))
                )
                intent = CompactionIntent(
                    str(USER_ID),
                    str(USER_ID),
                    0,
                    None,
                    generation,
                    summary,
                    1,
                    "b" * 64,
                    "c" * 64,
                )

                async def commit(run_id, call_id, completion, revision, draft):
                    call = await repo.update_model_call(
                        call_id, ModelCallStatus.COMPLETED, **completion
                    )
                    return call, Mock(), ()

                cast(Mock, context.checkpoints).commit_summary = AsyncMock(side_effect=commit)
                await context._summarize(
                    run_record(),
                    MODEL,
                    cast(StreamingProviderAdapter, provider),
                    summary,
                    "key",
                    "user",
                    1,
                    intent=intent,
                )

    asyncio.run(exercise())
    assert repo.calls[0].accounted_total_tokens == 110
    assert repo.calls[0].estimated_cost == pytest.approx(0.0008)
