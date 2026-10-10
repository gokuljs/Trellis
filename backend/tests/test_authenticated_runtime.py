import asyncio
from pathlib import Path
from threading import Event, Timer
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.auth import VerifiedUser
from app.api.routes import runtime
from app.core.config import Settings
from app.main import create_app


class StubVerifier:
    async def verify_bearer(self, authorization: str | None) -> VerifiedUser:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401)
        return await self.verify_token(authorization.removeprefix("Bearer "))

    async def verify_token(self, token: str) -> VerifiedUser:
        if token == "alice":
            return VerifiedUser(UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"), "alice@example.com")
        if token == "bob":
            return VerifiedUser(UUID("d7a9cd14-6abd-42c4-b46d-2f5bb82e7231"), "bob@example.com")
        raise HTTPException(status_code=401)


class ChangingVerifier(StubVerifier):
    def __init__(self) -> None:
        self.revoked = Event()
        self.changed_user = Event()

    async def verify_token(self, token: str) -> VerifiedUser:
        if self.revoked.is_set():
            raise HTTPException(status_code=401)
        if self.changed_user.is_set():
            return VerifiedUser(UUID("b47a02f5-660b-4f9c-9be4-52c46a7e98ce"), "bob@example.com")
        return await super().verify_token(token)


def test_runtime_websocket_rejects_untrusted_origin(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=StubVerifier())

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/runtime", headers={"origin": "https://untrusted.example"}),
    ):
        pass


def test_runtime_websocket_requires_auth_before_run_rpc(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=StubVerifier())

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as socket,
    ):
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "run-before-auth",
                "method": "run.resume",
                "params": {"runId": "someone-elses-run", "afterSequence": 0},
            }
        )
        reply = socket.receive_json()
        assert reply["error"]["data"]["code"] == "auth_required"


def test_runtime_websocket_authenticates_before_run_rpc(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=StubVerifier())

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as socket,
    ):
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "auth",
                "method": "auth.authenticate",
                "params": {"accessToken": "alice"},
            }
        )
        reply = socket.receive_json()
        assert reply["result"] == {"userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"}


def test_runtime_websocket_reports_account_in_use(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)
    first_app = create_app(settings, auth_verifier=StubVerifier())
    second_app = create_app(settings, auth_verifier=StubVerifier())
    auth_frame = {
        "jsonrpc": "2.0",
        "id": "auth",
        "method": "auth.authenticate",
        "params": {"accessToken": "alice"},
    }

    with (
        TestClient(first_app) as first_client,
        TestClient(second_app) as second_client,
        first_client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as first_socket,
    ):
        first_socket.send_json(auth_frame)
        assert first_socket.receive_json()["result"] == {
            "userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"
        }
        with second_client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as second_socket:
            second_socket.send_json(auth_frame)
            reply = second_socket.receive_json()
            assert reply["error"]["data"]["code"] == "account_in_use"
            with pytest.raises(WebSocketDisconnect) as closed:
                second_socket.receive_json()
            assert closed.value.code == 4409


@pytest.mark.parametrize("change", ["revoked", "changed_user"])
def test_runtime_websocket_closes_idle_connection_when_auth_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    verifier = ChangingVerifier()
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=verifier)
    monkeypatch.setattr(runtime, "_AUTH_RECHECK_SECONDS", 0.05, raising=False)

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as socket,
    ):
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "auth",
                "method": "auth.authenticate",
                "params": {"accessToken": "alice"},
            }
        )
        assert socket.receive_json()["result"] == {"userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"}
        getattr(verifier, change).set()
        # A delayed request bounds this test if idle revalidation regresses.
        probe = Timer(
            0.5,
            socket.send_json,
            args=(
                {
                    "jsonrpc": "2.0",
                    "id": "probe",
                    "method": "run.resume",
                    "params": {"runId": "missing", "afterSequence": 0},
                },
            ),
        )
        probe.start()
        try:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401
            assert probe.is_alive(), "Idle auth must close before the delayed request"
        finally:
            probe.cancel()


def test_runtime_websocket_allows_only_configured_web_origin(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            environment="production",
            data_dir=tmp_path,
            web_origin="https://app.example.com",
        ),
        auth_verifier=StubVerifier(),
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/runtime", headers={"origin": "https://app.example.com"}
        ) as socket:
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "auth",
                    "method": "auth.authenticate",
                    "params": {"accessToken": "alice"},
                }
            )
            assert socket.receive_json()["result"] == {
                "userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"
            }
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/runtime", headers={"origin": "https://app.example.com.attacker.test"}
            ),
        ):
            pass
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/runtime", headers={"origin": "http://localhost:3000"}),
        ):
            pass


@pytest.mark.parametrize(
    "configured_origin",
    ["http://app.example.com", "https://*.example.com", "https://app.example.com/path"],
)
def test_runtime_websocket_rejects_invalid_production_origin_config(
    tmp_path: Path, configured_origin: str
) -> None:
    app = create_app(
        Settings(environment="production", data_dir=tmp_path, web_origin=configured_origin),
        auth_verifier=StubVerifier(),
    )

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/runtime", headers={"origin": configured_origin}),
    ):
        pass


def test_runtime_websocket_rechecks_auth_before_dispatching_run_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = ChangingVerifier()
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=verifier)
    monkeypatch.setattr(runtime, "_AUTH_RECHECK_SECONDS", 60)

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as socket,
    ):
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "auth",
                "method": "auth.authenticate",
                "params": {"accessToken": "alice"},
            }
        )
        assert socket.receive_json()["result"] == {"userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"}
        verifier.revoked.set()
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "run-after-revocation",
                "method": "run.resume",
                "params": {"runId": "missing", "afterSequence": 0},
            }
        )
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_runtime_websocket_rechecks_auth_between_batched_run_rpcs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = ChangingVerifier()
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=verifier)
    monkeypatch.setattr(runtime, "_AUTH_RECHECK_SECONDS", 60)

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as socket,
    ):
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": "auth",
                "method": "auth.authenticate",
                "params": {"accessToken": "alice"},
            }
        )
        assert socket.receive_json()["result"] == {"userId": "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"}

        account = app.state.account_registry._contexts[UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5")]
        original_get_run = account.run_service.get_run
        first_lookup = True

        async def revoke_after_first_lookup(run_id: str):
            nonlocal first_lookup
            result = await original_get_run(run_id)
            if first_lookup:
                first_lookup = False
                verifier.revoked.set()
            return result

        monkeypatch.setattr(account.run_service, "get_run", revoke_after_first_lookup)
        socket.send_json(
            [
                {
                    "jsonrpc": "2.0",
                    "id": "first",
                    "method": "run.resume",
                    "params": {"runId": "missing-first", "afterSequence": 0},
                },
                {
                    "jsonrpc": "2.0",
                    "id": "second",
                    "method": "run.resume",
                    "params": {"runId": "missing-second", "afterSequence": 0},
                },
            ]
        )
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_runtime_websocket_cannot_access_another_accounts_run(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path), auth_verifier=StubVerifier())
    bob_id = UUID("d7a9cd14-6abd-42c4-b46d-2f5bb82e7231")

    with TestClient(app) as client:
        session = client.post("/api/sessions", headers={"Authorization": "Bearer bob"})
        assert session.status_code == 201
        bob_database = app.state.account_registry._contexts[bob_id].database

        async def create_bob_run():
            models = await bob_database.list_models()
            return await bob_database.create_run(
                session.json()["id"], "bob-turn", "bob-request", "Bob's message", models[0]
            )

        bob_run_id = asyncio.run(create_bob_run()).id

        with client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as alice_socket:
            alice_socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "alice-auth",
                    "method": "auth.authenticate",
                    "params": {"accessToken": "alice"},
                }
            )
            assert alice_socket.receive_json()["result"]["userId"] == (
                "a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5"
            )
            for method, extra in (
                ("run.resume", {}),
                ("run.cancel", {}),
                ("run.respond", {"toolCallId": "unknown", "decision": "approved"}),
            ):
                alice_socket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": method,
                        "method": method,
                        "params": {"runId": bob_run_id, "afterSequence": 0} | extra,
                    }
                )
                reply = alice_socket.receive_json()
                assert reply["id"] == method
                assert reply["error"]["data"]["code"] == "run_not_found"

        with client.websocket_connect(
            "/api/runtime", headers={"origin": "http://localhost:3000"}
        ) as bob_socket:
            bob_socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "bob-auth",
                    "method": "auth.authenticate",
                    "params": {"accessToken": "bob"},
                }
            )
            assert bob_socket.receive_json()["result"]["userId"] == str(bob_id)
            bob_socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "bob-resume",
                    "method": "run.resume",
                    "params": {"runId": bob_run_id, "afterSequence": 0},
                }
            )
            while True:
                reply = bob_socket.receive_json()
                if reply.get("id") == "bob-resume":
                    assert reply["result"]["runId"] == bob_run_id
                    break
