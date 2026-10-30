import asyncio
import json

import httpx
import pytest
from web_fixtures import RESERVATION, repository, scope

from app.application.errors import ApplicationError


def test_search_reservation_uses_owned_lease_and_retries_only_same_request():
    async def exercise():
        payloads = []

        def handler(request):
            assert request.url.path.endswith("/rpc/web_reserve_search_attempt")
            payloads.append(json.loads(request.content))
            if len(payloads) == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"reserved": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await repository(client).reserve_search_attempt(scope(), RESERVATION, "a" * 64, 1000)
        assert len(payloads) == 2 and payloads[0] == payloads[1]
        assert payloads[0] == {
            "p_payload": scope().to_dict(),
            "p_request_id": RESERVATION,
            "p_key_hash": "a" * 64,
            "p_monthly_limit": 1000,
            "p_capability": "private-run-capability",
        }

    asyncio.run(exercise())


def test_monthly_search_limit_is_safely_reported_without_retry():
    async def exercise():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                400, json={"code": "22023", "message": "web_search_budget_exhausted"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ApplicationError) as error:
                await repository(client).reserve_search_attempt(scope(), RESERVATION, "a" * 64, 2)
        assert error.value.code == "web_search_budget_exhausted" and calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload", [{"reserved": 1}, {"reserved": False}, {"reserved": True, "attempts": 999}, None]
)
def test_meter_response_must_not_expose_or_accept_ambiguous_global_data(payload):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            with pytest.raises(RuntimeError):
                await repository(client).reserve_search_attempt(scope(), RESERVATION, "a" * 64, 2)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload,ready",
    [
        ({"schema_version": 3, "ready": True}, True),
        ({"schema_version": 2, "ready": True}, False),
        ({"schema_version": 1, "ready": True}, False),
        ({"schema_version": 3, "ready": False}, False),
        ({"schema_version": True, "ready": True}, False),
        ({"schema_version": 3, "ready": 1}, False),
        ({"schema_version": 3, "ready": True, "global_attempts": 3}, False),
        (None, False),
    ],
)
def test_only_current_complete_schema_advertises_web_tools(payload, ready):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            assert await repository(client).web_tools_ready() is ready

    asyncio.run(exercise())


def test_missing_readiness_rpc_fails_closed():
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, json={"code": "PGRST202"})
            )
        ) as client:
            assert not await repository(client).web_tools_ready()

    asyncio.run(exercise())
