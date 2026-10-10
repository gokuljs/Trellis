"""Authenticated transport and account paths for existing API behavior tests.

Security tests use the real TestClient directly so they can exercise missing or
invalid credentials. This helper authenticates only tests of other behavior.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient as FastAPITestClient
from starlette.testclient import WebSocketTestSession

from app.api.auth import VerifiedUser
from app.core.config import Settings
from app.infrastructure.accounts import AccountContext
from app.main import create_app as production_create_app

TEST_USER_ID = UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5")
TEST_EMAIL = "alice@example.com"
TEST_TOKEN = "alice"


class StubVerifier:
    async def verify_bearer(self, authorization: str | None) -> VerifiedUser:
        if authorization != f"Bearer {TEST_TOKEN}":
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        return VerifiedUser(TEST_USER_ID, TEST_EMAIL)

    async def verify_token(self, token: str) -> VerifiedUser:
        return await self.verify_bearer(f"Bearer {token}")


def create_app(*args: Any, **kwargs: Any) -> FastAPI:
    kwargs.setdefault("auth_verifier", StubVerifier())
    kwargs.setdefault("legacy_sqlite_for_tests", True)
    return production_create_app(*args, **kwargs)


class TestClient(FastAPITestClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.headers.setdefault("Authorization", f"Bearer {TEST_TOKEN}")

    @contextmanager
    def websocket_connect(  # type: ignore[override]
        self, url: str, subprotocols: list[str] | None = None, **kwargs: Any
    ) -> Iterator[WebSocketTestSession]:
        headers = {"origin": "http://localhost:3000"}
        headers.update(kwargs.pop("headers", {}))
        with super().websocket_connect(
            url, subprotocols=subprotocols, headers=headers, **kwargs
        ) as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "test-auth",
                    "method": "auth.authenticate",
                    "params": {"accessToken": TEST_TOKEN},
                }
            )
            response = websocket.receive_json()
            assert response == {
                "jsonrpc": "2.0",
                "id": "test-auth",
                "result": {"userId": str(TEST_USER_ID)},
            }
            yield websocket


def account_path(settings: Settings) -> Path:
    return settings.data_dir / "accounts" / str(TEST_USER_ID)


def account_database_path(settings: Settings) -> Path:
    return account_path(settings) / "state.db"


def account_secrets_path(settings: Settings) -> Path:
    return account_path(settings) / ".env"


def account_context(application: FastAPI) -> AccountContext:
    return application.state.account_registry._contexts[TEST_USER_ID]
