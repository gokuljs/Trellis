import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.infrastructure.database import Database
from tests.support import TestClient, account_database_path, create_app


def test_session_workspace_is_canonical_and_survives_restart(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "project-link"
    alias.symlink_to(project, target_is_directory=True)

    with TestClient(create_app(settings)) as client:
        ordinary = client.post("/api/sessions")
        attached = client.post("/api/sessions", json={"workspace_path": str(alias)})
        switched = client.put(
            f"/api/sessions/{attached.json()['id']}/workspace",
            json={"workspace_path": str(tmp_path)},
        )

    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/api/sessions/{attached.json()['id']}")
        listed = client.get("/api/sessions")
        cleared = client.put(
            f"/api/sessions/{attached.json()['id']}/workspace",
            json={"workspace_path": None},
        )

    assert ordinary.status_code == 201
    assert ordinary.json()["workspace_path"] is None
    assert attached.status_code == 201
    assert attached.json()["workspace_path"] == str(project.resolve())
    assert switched.status_code == 200
    assert switched.json()["workspace_path"] == str(tmp_path.resolve())
    assert detail.json()["session"]["workspace_path"] == str(tmp_path.resolve())
    assert {item["id"]: item["workspace_path"] for item in listed.json()}[
        attached.json()["id"]
    ] == str(tmp_path.resolve())
    assert cleared.status_code == 200
    assert cleared.json()["workspace_path"] is None


def test_synced_workspace_requires_selection_on_each_backend_instance(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()

    with TestClient(create_app(settings)) as client:
        attached = client.post("/api/sessions", json={"workspace_path": str(project)})
        assert attached.json()["workspace_ready"] is True

    with TestClient(create_app(settings)) as client:
        session_id = attached.json()["id"]
        detail = client.get(f"/api/sessions/{session_id}")
        listed = client.get("/api/sessions")
        assert detail.json()["session"]["workspace_path"] == str(project)
        assert detail.json()["session"]["workspace_ready"] is False
        assert listed.json()[0]["workspace_ready"] is False

        selected = client.put(
            f"/api/sessions/{session_id}/workspace",
            json={"workspace_path": str(project)},
        )
        assert selected.json()["workspace_ready"] is True
        project.rmdir()
        assert (
            client.get(f"/api/sessions/{session_id}").json()["session"]["workspace_ready"] is False
        )


@pytest.mark.parametrize("workspace_path", ["relative/path", "missing", "file"])
def test_session_creation_rejects_invalid_workspace_paths(
    tmp_path: Path, workspace_path: str
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    file_path = tmp_path / "file"
    file_path.write_text("data")
    supplied = {
        "relative/path": "project",
        "missing": str(tmp_path / "absent"),
        "file": str(file_path),
    }[workspace_path]

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"workspace_path": supplied})
        sessions = client.get("/api/sessions")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workspace"
    assert sessions.json() == []


def test_workspace_cannot_change_during_an_active_run(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()
    database = Database(account_database_path(settings))

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={"workspace_path": str(project)}).json()

        async def queue_run() -> None:
            await database.create_run(
                session["id"],
                "turn-with-workspace",
                "request-with-workspace",
                "Inspect this project",
                (await database.list_models())[0],
            )

        asyncio.run(queue_run())
        response = client.put(
            f"/api/sessions/{session['id']}/workspace",
            json={"workspace_path": None},
        )
        detail = client.get(f"/api/sessions/{session['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_workspace_busy"
    assert detail.json()["session"]["workspace_path"] == str(project)
