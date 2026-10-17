import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

import app.api.auth as auth_module
from app.api.auth import SupabaseAuthVerifier, VerifiedUser
from app.core.config import Settings

USER_ID = "715f11e0-38e5-48bd-b63a-8c7980a9bd55"
SUPABASE_URL = "https://example.supabase.co"


def access_token(
    *, user_id: str = USER_ID, expires_at: datetime | None = None, role: str = "authenticated"
) -> str:
    expiry = expires_at or datetime.now(UTC) + timedelta(hours=1)
    claims = {"sub": user_id, "exp": int(expiry.timestamp()), "role": role}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def settings() -> Settings:
    return Settings(
        environment="test",
        supabase_url=SUPABASE_URL,
        supabase_publishable_key="sb_publishable_test",
    )


def auth_user(*, user_id: str = USER_ID, email: object = "user@example.com") -> dict[str, object]:
    return {
        "id": user_id,
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "email_confirmed_at": "2026-10-01T00:00:00Z",
        "phone": "",
        "last_sign_in_at": "2026-10-10T00:00:00Z",
        "app_metadata": {"provider": "google", "providers": ["google"]},
        "user_metadata": {},
        "identities": [],
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-10T00:00:00Z",
    }


def test_verifier_returns_auth_user_from_fixed_endpoint_without_url_token() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=auth_user())

    async def verify() -> VerifiedUser:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await SupabaseAuthVerifier(settings(), client).verify_bearer(
                f"Bearer {access_token()}"
            )

    user = asyncio.run(verify())

    assert user.id == UUID(USER_ID)
    assert user.email == "user@example.com"
    assert user.expires_at is not None and user.expires_at > datetime.now(UTC)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://example.supabase.co/auth/v1/user"
    assert requests[0].headers["apikey"] == "sb_publishable_test"
    assert requests[0].headers["authorization"].startswith("Bearer header.")
    assert "access-token" not in str(requests[0].url)


@pytest.mark.parametrize(
    "header",
    [None, "", "access-token", "Basic access-token", "Bearer", "Bearer ", "Bearer x y"],
)
def test_verifier_rejects_missing_or_malformed_bearer_without_network(header: str | None) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid credentials must not contact Auth")

    async def verify() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            await SupabaseAuthVerifier(settings(), client).verify_bearer(header)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 401
    assert "access-token" not in str(caught.value.detail)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_verifier_rejects_token_when_auth_denies_it(status: int) -> None:
    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status))
        ) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 401
    assert "access-token" not in str(caught.value.detail)


@pytest.mark.parametrize("status", [404, 429, 500, 503])
def test_verifier_fails_closed_when_auth_cannot_validate(status: int) -> None:
    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status))
        ) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 503
    assert "access-token" not in str(caught.value.detail)


def test_verifier_fails_closed_on_auth_network_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    async def verify() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 503


def test_verifier_fails_closed_when_auth_response_exceeds_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module, "_AUTH_DEADLINE_SECONDS", 0.01, raising=False)

    class SlowAuthBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            encoded = json.dumps(auth_user()).encode()
            midpoint = len(encoded) // 2
            yield encoded[:midpoint]
            await asyncio.sleep(0.05)
            yield encoded[midpoint:]

    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=SlowAuthBody())
            )
        ) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 503


@pytest.mark.parametrize("payload", [{"id": "invalid", "email": "user@example.com"}, [], {}])
def test_verifier_rejects_malformed_auth_user(payload: object) -> None:
    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 503


@pytest.mark.parametrize(
    "url,key",
    [
        (None, "sb_publishable_test"),
        (SUPABASE_URL, None),
        ("http://example.supabase.co", "sb_publishable_test"),
        ("https://other.example.com", "sb_publishable_test"),
        ("https://127.0.0.1", "sb_publishable_test"),
        ("https://localhost", "sb_publishable_test"),
        ("https://supabase.co", "sb_publishable_test"),
        ("https://nested.project.supabase.co", "sb_publishable_test"),
        ("https://example.supabase.co:8443", "sb_publishable_test"),
        ("https://name:password@example.supabase.co", "sb_publishable_test"),
        ("https://example.supabase.co/?token=abc", "sb_publishable_test"),
        ("https://example.supabase.co/#fragment", "sb_publishable_test"),
        (SUPABASE_URL, "sb_secret_not-allowed"),
        (SUPABASE_URL, "arbitrary-key"),
        (SUPABASE_URL, "sb_publishable_"),
        (SUPABASE_URL, "sb_publishable_bad value"),
        (SUPABASE_URL, "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.signature"),
        (
            SUPABASE_URL,
            "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.signature",
        ),
        (SUPABASE_URL, "eyJhbGciOiJIUzI1NiJ9.!.signature"),
    ],
)
def test_verifier_fails_closed_for_missing_or_unsafe_configuration(
    url: str | None, key: str | None
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Unsafe configuration must not contact Auth")

    async def verify() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            configured = Settings(
                environment="development",
                supabase_url=url,
                supabase_publishable_key=key,
            )
            await SupabaseAuthVerifier(configured, client).verify_token("access-token")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 503


def test_verifier_accepts_local_http_in_development() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=auth_user(email=None))

    async def verify() -> VerifiedUser:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            configured = Settings(
                environment="development",
                supabase_url="http://127.0.0.1:54321/",
                supabase_publishable_key="sb_publishable_local",
            )
            return await SupabaseAuthVerifier(configured, client).verify_token(access_token())

    user = asyncio.run(verify())

    assert user.email is None
    assert str(requests[0].url) == "http://127.0.0.1:54321/auth/v1/user"


@pytest.mark.parametrize(
    "token",
    [
        "access-token",
        access_token(user_id="d7a9cd14-6abd-42c4-b46d-2f5bb82e7231"),
        access_token(expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
        access_token(role="service_role"),
    ],
)
def test_verifier_rejects_unusable_postgrest_access_token(token: str) -> None:
    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=auth_user()))
        ) as client:
            await SupabaseAuthVerifier(settings(), client).verify_token(token)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(verify())

    assert caught.value.status_code == 401
