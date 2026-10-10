from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.auth import VerifiedUser
from app.core.config import Settings
from app.main import create_app


class StubVerifier:
    async def verify_bearer(self, authorization: str | None) -> VerifiedUser:
        if authorization == "Bearer alice":
            return VerifiedUser(UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"), "alice@example.com")
        if authorization == "Bearer bob":
            return VerifiedUser(UUID("d7a9cd14-6abd-42c4-b46d-2f5bb82e7231"), "bob@example.com")
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    async def verify_token(self, token: str) -> VerifiedUser:
        return await self.verify_bearer(f"Bearer {token}")


def test_private_api_requires_bearer_token(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/profile").status_code == 401
        assert client.post("/api/workspaces/pick").status_code == 401


def test_verified_accounts_cannot_read_each_others_chats(tmp_path: Path) -> None:
    app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        auth_verifier=StubVerifier(),
        legacy_sqlite_for_tests=True,
    )

    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        bob = {"Authorization": "Bearer bob"}
        created = client.post("/api/sessions", headers=alice)
        assert created.status_code == 201
        chat_id = created.json()["id"]

        assert [chat["id"] for chat in client.get("/api/sessions", headers=alice).json()] == [
            chat_id
        ]
        assert client.get("/api/sessions", headers=bob).json() == []
        assert client.get(f"/api/sessions/{chat_id}", headers=bob).status_code == 404
        assert client.get("/api/profile", headers=alice).json()["id"] == str(
            UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5")
        )


def test_profile_email_is_owned_by_verified_auth_user(tmp_path: Path) -> None:
    app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        auth_verifier=StubVerifier(),
        legacy_sqlite_for_tests=True,
    )

    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        assert client.get("/api/profile", headers=alice).json()["email"] == "alice@example.com"
        changed = client.put(
            "/api/profile",
            headers=alice,
            json={"display_name": "Alice", "email": "attacker@example.com"},
        )
        assert changed.status_code == 422
        updated = client.put("/api/profile", headers=alice, json={"display_name": "Alice"})
        assert updated.status_code == 200
        assert updated.json()["email"] == "alice@example.com"


def test_onboarding_uses_verified_email_not_submitted_email(tmp_path: Path) -> None:
    app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        auth_verifier=StubVerifier(),
        legacy_sqlite_for_tests=True,
    )

    with TestClient(app) as client:
        alice = {"Authorization": "Bearer alice"}
        assert client.put("/api/onboarding/steps/intro", headers=alice).status_code == 200
        tampered = client.put(
            "/api/onboarding/steps/profile",
            headers=alice,
            json={"display_name": "Alice", "email": "attacker@example.com"},
        )
        assert tampered.status_code == 422
        saved = client.put(
            "/api/onboarding/steps/profile",
            headers=alice,
            json={"display_name": "Alice"},
        )
        assert saved.status_code == 200
        assert client.get("/api/profile", headers=alice).json()["email"] == "alice@example.com"
