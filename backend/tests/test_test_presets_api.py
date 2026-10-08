from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_test_presets_are_saved_for_workspace_and_survive_restart(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    workspace = tmp_path / "project"
    (workspace / "backend").mkdir(parents=True)

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()
        saved = client.post(
            f"/api/sessions/{first['id']}/test-presets",
            json={"name": "Backend", "command": "python -m pytest", "cwd": "backend"},
        )
        second = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()

    with TestClient(create_app(settings)) as client:
        listed = client.get(f"/api/sessions/{second['id']}/test-presets")
        removed = client.delete(f"/api/sessions/{second['id']}/test-presets/Backend")
        after_delete = client.get(f"/api/sessions/{first['id']}/test-presets")

    assert saved.status_code == 201
    assert saved.json()["name"] == "Backend"
    assert listed.status_code == 200
    assert listed.json() == [{"name": "Backend", "command": "python -m pytest", "cwd": "backend"}]
    assert removed.status_code == 204
    assert after_delete.json() == []


def test_test_preset_requires_workspace_and_rejects_invalid_command(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    workspace = tmp_path / "project"
    workspace.mkdir()
    with TestClient(create_app(settings)) as client:
        ordinary = client.post("/api/sessions").json()
        attached = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()
        absent = client.post(
            f"/api/sessions/{ordinary['id']}/test-presets",
            json={"name": "Unsafe", "command": "python -m pytest"},
        )
        unsafe = client.post(
            f"/api/sessions/{attached['id']}/test-presets",
            json={"name": "Unsafe", "command": "python -m pytest; rm -rf ."},
        )
        bad_cwd = client.post(
            f"/api/sessions/{attached['id']}/test-presets",
            json={"name": "Unsafe", "command": "python -m pytest", "cwd": "../other"},
        )
        secret = client.post(
            f"/api/sessions/{attached['id']}/test-presets",
            json={"name": "Unsafe", "command": "python --api-key=secret-value"},
        )
        listed = client.get(f"/api/sessions/{attached['id']}/test-presets")

    assert absent.status_code == 409
    assert unsafe.status_code == 422
    assert bad_cwd.status_code == 422
    assert secret.status_code == 422
    assert listed.json() == []


def test_invalid_test_command_does_not_echo_submitted_secret(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    workspace = tmp_path / "project"
    workspace.mkdir()
    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()
        response = client.post(
            f"/api/sessions/{session['id']}/test-presets",
            json={
                "name": "Unsafe",
                "command": "python --api-key=secret-value " + "x" * 2100,
            },
        )

    assert response.status_code == 422
    assert "secret-value" not in response.text
