from pathlib import Path
from uuid import uuid4

from tests.support import TestClient, create_app, make_settings


def test_durable_mode_retires_sync_execution_but_keeps_history(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(update={"durable_execution_enabled": True})
    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions").json()
        response = client.post(
            f"/api/sessions/{session['id']}/turns",
            json={"turn_id": str(uuid4()), "content": "Hello"},
        )
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "synchronous_execution_retired"
        assert "run.start" in response.json()["error"]["message"]
        history = client.get(f"/api/sessions/{session['id']}")
        assert history.status_code == 200
        assert history.json()["messages"] == []
        assert (
            "410"
            in client.get("/openapi.json").json()["paths"]["/api/sessions/{session_id}/turns"][
                "post"
            ]["responses"]
        )
