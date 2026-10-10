from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.mark.parametrize(
    "method,suffix",
    [("get", ""), ("post", ""), ("delete", "/Backend")],
)
def test_saved_test_command_routes_are_unavailable(
    tmp_path: Path, method: str, suffix: str
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path / "data")

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions").json()
        response = getattr(client, method)(f"/api/sessions/{session['id']}/test-presets{suffix}")

    assert response.status_code == 404
