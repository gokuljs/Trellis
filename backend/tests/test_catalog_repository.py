import asyncio
import json
from dataclasses import asdict

import httpx

from app.application.model_catalog import profile_for_model
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.support import verified_token_source
from tests.test_supabase_repository import ALICE, SETTINGS


def test_catalog_read_includes_pending_but_runtime_read_requires_ready_metadata() -> None:
    profile = profile_for_model("openai:gpt-6.1-sol")
    assert profile is not None
    ready: dict[str, object] = {
        "id": profile.id,
        "provider_id": profile.provider_id,
        "provider_name": "OpenAI",
        "adapter_kind": "openai",
        "upstream_model_id": profile.upstream_model_id,
        "name": profile.name,
        "requires_api_key": True,
        "supports_streaming": True,
        "supports_tools": True,
        "enabled": True,
        "metadata_status": "ready",
        "trusted_metadata": json.loads(json.dumps(asdict(profile))),
        "metadata_verified_at": "2026-10-15T00:00:00Z",
        "availability": "available",
        "availability_checked_at": "2026-10-15T00:10:00Z",
    }
    pending = dict(
        ready,
        id="openai:future",
        upstream_model_id="future",
        enabled=False,
        metadata_status="pending",
        trusted_metadata=None,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=[ready] if "enabled" in request.url.params else [ready, pending]
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            models = await repository.list_catalog_models()
            assert len(models) == 2 and models[1].metadata_status == "pending"
            assert models[0].context_window_tokens == profile.context_window_tokens
            assert models[0].metadata_verified_at == "2026-10-15T00:00:00Z"
            assert models[0].availability == "available"
            assert [model.id for model in await repository.list_models()] == [profile.id]

    asyncio.run(exercise())
    assert requests[1].url.params["metadata_status"] == "eq.ready"
    assert all("user_id" not in request.url.params for request in requests)


def test_catalog_does_not_trust_modified_metadata_or_unknown_enabled_upstream() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=[
                        {
                            "id": "openai:gpt-5.5",
                            "provider_id": "openai",
                            "provider_name": "OpenAI",
                            "adapter_kind": "openai",
                            "upstream_model_id": "not-gpt-5.5",
                            "name": "Spoofed",
                            "requires_api_key": True,
                            "supports_streaming": True,
                            "supports_tools": True,
                            "enabled": True,
                            "metadata_status": "ready",
                            "trusted_metadata": {"context_window_tokens": 999999},
                        }
                    ],
                )
            )
        ) as client:
            repository = SupabaseRepository(SETTINGS, ALICE, verified_token_source(ALICE), client)
            models = await repository.list_models()
            assert models == []
            pending = (await repository.list_catalog_models())[0]
            assert pending.metadata_status == "pending" and not pending.enabled

    asyncio.run(exercise())
