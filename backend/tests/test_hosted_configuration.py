import asyncio
import warnings
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from starlette.exceptions import StarletteDeprecationWarning
from starlette.websockets import WebSocketDisconnect

from app.api.auth import VerifiedUser
from app.core.config import Settings
from app.core.origins import allowed_web_origins
from app.infrastructure import readiness
from app.infrastructure.readiness import SupabaseReadiness
from app.infrastructure.runtime_events import RuntimeEventHub
from app.main import create_app

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_ID = UUID("00000000-0000-4000-8000-000000000002")
ORIGIN = "https://trellis.example.com"


def hosted_settings(
    *,
    environment: Literal["development", "test", "preview", "production"] = "production",
    web_origin: str | None = ORIGIN,
    private_user_ids: Iterable[UUID | str] = (USER_ID,),
) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        web_origin=web_origin,
        private_user_ids=private_user_ids,
    )


@pytest.mark.parametrize("environment", ["preview", "production"])
def test_hosted_access_configuration_fails_closed(environment) -> None:
    with pytest.raises(ValidationError, match="Hosted environments require"):
        hosted_settings(environment=environment, private_user_ids=[])
    with pytest.raises(ValidationError, match="Hosted environments require"):
        hosted_settings(environment=environment, web_origin=None)
    with pytest.raises(ValidationError):
        hosted_settings(environment=environment, private_user_ids=["someone@example.com"])


@pytest.mark.parametrize(
    "origin",
    [
        "https://trellis.example.com/",
        "https://*.example.com",
        "http://trellis.example.com",
        "https://trellis.example.com/path",
        "https://trellis.example.com?key=secret",
        "https://user:secret@trellis.example.com",
        "https://Trellis.example.com",
        "https://trellis.example.com:443",
        "https://trellis.example.com:bad",
    ],
)
def test_noncanonical_web_origins_are_rejected(origin) -> None:
    with pytest.raises(ValidationError):
        hosted_settings(web_origin=origin)


def test_http_cors_uses_the_same_exact_origin_policy_as_websockets() -> None:
    app = create_app(hosted_settings())
    assert allowed_web_origins("production", ORIGIN) == frozenset({ORIGIN})
    with TestClient(app) as client:
        for origin, status in [
            (ORIGIN, 200),
            ("https://preview.example.com", 400),
            ("http://localhost:3000", 400),
            ("null", 400),
        ]:
            response = client.options(
                "/api/profile",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            assert response.status_code == status
            assert response.headers.get("Access-Control-Allow-Origin") == (
                ORIGIN if status == 200 else None
            )
            assert "Access-Control-Allow-Credentials" not in response.headers
        for headers in [
            {"Access-Control-Request-Method": "PATCH"},
            {"Access-Control-Request-Headers": "x-private-header"},
            {"Access-Control-Request-Headers": "traceparent"},
        ]:
            assert (
                client.options(
                    "/api/profile",
                    headers={
                        "Origin": ORIGIN,
                        "Access-Control-Request-Method": "GET",
                        **headers,
                    },
                ).status_code
                == 400
            )
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect("/api/runtime", headers={"Origin": "http://localhost:3000"}),
        ):
            pass
        assert rejected.value.code == 1008


def install_account(app, user_id=USER_ID):
    verified = VerifiedUser(user_id, None)
    verifier = SimpleNamespace(
        verify_bearer=AsyncMock(return_value=verified),
        verify_token=AsyncMock(return_value=verified),
    )
    registry = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                run_service=SimpleNamespace(),
                event_hub=RuntimeEventHub(),
            )
        )
    )
    app.state.auth_verifier = verifier
    app.state.account_registry = registry
    return registry


def authenticate(client):
    websocket = client.websocket_connect("/api/runtime", headers={"Origin": ORIGIN})
    return websocket


def auth_frame():
    return {
        "jsonrpc": "2.0",
        "id": "auth",
        "method": "auth.authenticate",
        "params": {"accessToken": "test"},
    }


def test_private_access_denies_http_and_websocket_before_account_admission() -> None:
    app = create_app(hosted_settings())
    with TestClient(app) as client:
        registry = install_account(app, OTHER_ID)
        response = client.get(
            "/api/profile", headers={"Authorization": "Bearer test", "Origin": ORIGIN}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "private_access_denied"
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
        with authenticate(client) as websocket:
            websocket.send_json(auth_frame())
            assert websocket.receive_json()["error"]["data"] == {
                "code": "private_access_denied",
                "status": 403,
            }
            assert websocket.receive()["code"] == 4403
        registry.get.assert_not_awaited()


def test_private_access_is_rechecked_before_each_runtime_command() -> None:
    app = create_app(hosted_settings())
    with TestClient(app) as client:
        registry = install_account(app)
        with authenticate(client) as websocket:
            websocket.send_json(auth_frame())
            assert websocket.receive_json()["result"]["userId"] == str(USER_ID)
            app.state.settings.private_user_ids = frozenset({OTHER_ID})
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "run",
                    "method": "run.resume",
                    "params": {"runId": "run-1"},
                }
            )
            assert websocket.receive_json()["error"]["data"] == {
                "code": "private_access_denied",
                "status": 403,
            }
            assert websocket.receive()["code"] == 4403
        assert registry.get.await_count == 1


def test_readiness_requires_database_contract_and_never_requires_sign_in() -> None:
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        verifier = SimpleNamespace(verify_bearer=AsyncMock(side_effect=AssertionError))
        app.state.auth_verifier = verifier
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 503
        app.state.readiness_probe = SimpleNamespace(is_ready=AsyncMock(return_value=True))
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
        assert response.headers["Cache-Control"] == "no-store"
        app.state.readiness_probe = SimpleNamespace(
            is_ready=AsyncMock(side_effect=RuntimeError("secret-canary"))
        )
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        verifier.verify_bearer.assert_not_awaited()


def test_readiness_uses_only_the_anonymous_schema_probe() -> None:
    requests = []

    async def handle(request):
        requests.append(request)
        assert request.url == "https://example.supabase.co/rest/v1/rpc/readiness"
        assert request.method == "POST"
        assert request.headers["Content-Profile"] == "trellis"
        assert request.headers["apikey"] == "sb_publishable_test"
        assert "Authorization" not in request.headers
        assert request.content == b"{}"
        return httpx.Response(200, json={"status": "ready"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            probe = SupabaseReadiness(
                Settings(
                    _env_file=None,
                    supabase_url="https://example.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                client,
            )
            assert await probe.is_ready()

    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"status": "unavailable"}),
        httpx.Response(302, headers={"Location": "https://example.com"}),
    ],
)
def test_unavailable_readiness_responses_are_safe(response) -> None:
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            probe = SupabaseReadiness(
                Settings(
                    _env_file=None,
                    supabase_url="https://example.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                client,
            )
            assert not await probe.is_ready()

    asyncio.run(exercise())


def test_readiness_deadline_is_bounded_and_cancellation_propagates(monkeypatch) -> None:
    monkeypatch.setattr(readiness, "_READINESS_TIMEOUT_SECONDS", 0.01)

    async def hanging(_request):
        await asyncio.Event().wait()

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
            probe = SupabaseReadiness(
                Settings(
                    _env_file=None,
                    supabase_url="https://example.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                client,
            )
            assert not await probe.is_ready()
            task = asyncio.create_task(probe.is_ready())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
