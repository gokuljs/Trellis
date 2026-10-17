import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.application.errors import ApplicationError
from app.application.sessions import SessionService
from app.application.workspaces import WorkspacePickerService
from app.core.config import Settings
from app.main import create_app
from tests.memory_repository import MemoryRepository, memory_repository_factory
from tests.support import TEST_USER_ID, StubVerifier, TestClient, account_repository_key


def chat_only_app(
    settings: Settings, *, workspace_picker: WorkspacePickerService | None = None
) -> FastAPI:
    return create_app(
        settings,
        auth_verifier=StubVerifier(),
        repository_factory=memory_repository_factory,
        workspace_picker=workspace_picker,
    )


@pytest.mark.parametrize("workspace_path", ["/private/host-folder", "relative/path", ""])
def test_chat_only_creation_denies_workspace_before_resolving_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_path: str
) -> None:
    resolved_paths: list[str] = []

    def record_resolution(path: str) -> str:
        resolved_paths.append(path)
        return path

    monkeypatch.setattr("app.application.sessions._resolved_workspace", record_resolution)
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    with TestClient(chat_only_app(settings)) as client:
        response = client.post("/api/sessions", json={"workspace_path": workspace_path})
        sessions = client.get("/api/sessions")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"
    assert resolved_paths == []
    assert sessions.json() == []


@pytest.mark.parametrize("payload", [None, {"workspace_path": None}])
def test_chat_only_creation_accepts_an_unattached_chat(
    tmp_path: Path, payload: dict[str, None] | None
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    with TestClient(chat_only_app(settings)) as client:
        response = client.post("/api/sessions", json=payload)

    assert response.status_code == 201
    assert response.json()["workspace_path"] is None
    assert response.json()["workspace_ready"] is False


@pytest.mark.parametrize("workspace_path", [None, "/private/host-folder"])
def test_chat_only_workspace_update_is_disabled_even_when_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_path: str | None
) -> None:
    resolved_paths: list[str] = []

    def record_resolution(path: str) -> str:
        resolved_paths.append(path)
        return path

    monkeypatch.setattr("app.application.sessions._resolved_workspace", record_resolution)
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    with TestClient(chat_only_app(settings)) as client:
        session = client.post("/api/sessions").json()
        response = client.put(
            f"/api/sessions/{session['id']}/workspace", json={"workspace_path": workspace_path}
        )
        detail = client.get(f"/api/sessions/{session['id']}")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"
    assert resolved_paths == []
    assert detail.json()["session"]["workspace_path"] is None


def test_chat_only_responses_hide_saved_workspace_metadata(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    repository = MemoryRepository(account_repository_key(settings), owner_id=TEST_USER_ID)
    session = asyncio.run(repository.create_session("/private/host-folder"))
    with TestClient(chat_only_app(settings)) as client:
        listed = client.get("/api/sessions")
        detail = client.get(f"/api/sessions/{session.id}")

    assert listed.json()[0]["workspace_path"] is None
    assert listed.json()[0]["workspace_ready"] is False
    assert detail.json()["session"]["workspace_path"] is None
    assert detail.json()["session"]["workspace_ready"] is False
    assert asyncio.run(repository.get_session(session.id)) == session


class RecordingPicker:
    def __init__(self) -> None:
        self.calls = 0

    async def pick_directory(self) -> str:
        self.calls += 1
        return "/private/host-folder"


def test_chat_only_picker_denies_authenticated_requests_without_invoking_picker(
    tmp_path: Path,
) -> None:
    picker = RecordingPicker()
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    with TestClient(
        chat_only_app(settings, workspace_picker=WorkspacePickerService(picker))
    ) as client:
        response = client.post("/api/workspaces/pick")
        client.headers.pop("Authorization")
        unauthenticated = client.post("/api/workspaces/pick")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"
    assert unauthenticated.status_code == 401
    assert picker.calls == 0


def test_default_session_service_keeps_saved_workspace_bindings_inert(tmp_path: Path) -> None:
    class HostBinding:
        async def resolve(self, session_id: str, workspace_path: str | None) -> Path:
            raise AssertionError("Chat-only mode must not inspect host paths")

        def bind(self, session_id: str, workspace_root: Path | None) -> None:
            raise AssertionError("Chat-only mode must not bind host paths")

    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "memory")
        saved = await repository.create_session("/private/host-folder")
        service = SessionService(repository, workspace_bindings=HostBinding())
        assert await service.workspace_ready(saved) is False
        created = await service.create()
        assert created.workspace_path is None

    asyncio.run(exercise())


def test_default_picker_service_denies_direct_use() -> None:
    picker = RecordingPicker()
    with pytest.raises(ApplicationError) as failure:
        asyncio.run(WorkspacePickerService(picker).pick_directory())
    assert failure.value.code == "capability_disabled"
    assert picker.calls == 0
