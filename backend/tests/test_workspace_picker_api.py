import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from tests.support import TestClient, create_app


class FakeFolderPicker:
    def __init__(
        self,
        result: str | None = None,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.result = result
        self.error = error
        self.block = block
        self.cancelled = False

    async def pick_directory(self) -> str | None:
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        return self.result


def make_picker_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    picker: FakeFolderPicker,
    *,
    timeout_seconds: float = 900,
) -> TestClient:
    monkeypatch.setattr("app.main.NativeFolderPicker", lambda: picker, raising=False)
    monkeypatch.setattr("app.main.WORKSPACE_PICKER_TIMEOUT_SECONDS", timeout_seconds, raising=False)
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    return TestClient(create_app(settings))


def test_workspace_picker_returns_the_selected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_picker_client(
        tmp_path, monkeypatch, FakeFolderPicker(result="/tmp/trellis-project")
    ) as client:
        response = client.post("/api/workspaces/pick")

    assert response.status_code == 200
    assert response.json() == {"path": "/tmp/trellis-project"}


def test_workspace_picker_returns_null_when_the_dialog_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_picker_client(tmp_path, monkeypatch, FakeFolderPicker()) as client:
        response = client.post("/api/workspaces/pick")

    assert response.status_code == 200
    assert response.json() == {"path": None}


def test_workspace_picker_reports_when_the_native_dialog_cannot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = FakeFolderPicker(error=OSError("Tk is unavailable"))
    with make_picker_client(tmp_path, monkeypatch, picker) as client:
        response = client.post("/api/workspaces/pick")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workspace_picker_unavailable"


def test_workspace_picker_times_out_and_cancels_the_pending_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = FakeFolderPicker(block=True)
    with make_picker_client(tmp_path, monkeypatch, picker, timeout_seconds=0.01) as client:
        response = client.post("/api/workspaces/pick")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "workspace_picker_timeout"
    assert picker.cancelled
