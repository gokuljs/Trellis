import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def make_settings(data_dir: Path) -> Settings:
    return Settings(environment="test", data_dir=data_dir)


def test_installation_profile_id_survives_application_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as first_client:
        first_response = first_client.get("/api/profile")

    with TestClient(create_app(settings)) as restarted_client:
        restarted_response = restarted_client.get("/api/profile")

    assert first_response.status_code == 200
    assert restarted_response.status_code == 200
    first_profile = first_response.json()
    restarted_profile = restarted_response.json()
    assert UUID(first_profile["id"])
    assert restarted_profile["id"] == first_profile["id"]
    assert first_profile["display_name"] is None
    assert first_profile["email"] is None


def test_separate_data_directories_receive_distinct_installation_ids(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path / "first"))) as first_client:
        first_id = first_client.get("/api/profile").json()["id"]
    with TestClient(create_app(make_settings(tmp_path / "second"))) as second_client:
        second_id = second_client.get("/api/profile").json()["id"]

    assert first_id != second_id


def test_profile_details_can_be_updated_and_restored(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/api/profile",
            json={"display_name": "Gokul", "email": "gokul@example.com"},
        )

    with TestClient(create_app(settings)) as restarted_client:
        restored = restarted_client.get("/api/profile")

    assert response.status_code == 200
    assert restored.json()["display_name"] == "Gokul"
    assert restored.json()["email"] == "gokul@example.com"


def test_api_keys_are_write_only_and_kept_outside_sqlite(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    secret = "sk-test-super-secret-7890"

    with TestClient(create_app(settings)) as client:
        saved = client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": secret},
        )
        visible_settings = client.get("/api/settings")

    assert saved.status_code == 200
    assert visible_settings.status_code == 200
    openai = next(
        provider for provider in visible_settings.json()["providers"] if provider["id"] == "openai"
    )
    assert openai["configured"] is True
    assert openai["key_hint"] == "••••7890"
    assert secret not in saved.text
    assert secret not in visible_settings.text
    assert secret.encode() not in settings.database_path.read_bytes()
    assert secret in settings.secrets_path.read_text()
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.secrets_path.stat().st_mode) == 0o600


def test_provider_selection_and_key_removal_are_persistent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        selected = client.put(
            "/api/settings/provider",
            json={"provider": "anthropic"},
        )
        client.put(
            "/api/settings/providers/anthropic/api-key",
            json={"api_key": "sk-ant-temporary"},
        )
        removed = client.delete("/api/settings/providers/anthropic/api-key")

    with TestClient(create_app(settings)) as restarted_client:
        restored = restarted_client.get("/api/settings")

    anthropic = next(
        provider for provider in restored.json()["providers"] if provider["id"] == "anthropic"
    )
    assert selected.status_code == 200
    assert removed.status_code == 200
    assert restored.json()["selected_provider"] == "anthropic"
    assert anthropic["configured"] is False
    assert anthropic["key_hint"] is None


def test_sessions_are_listed_by_recent_activity_and_survive_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/sessions")
        second = client.post("/api/sessions")

    with TestClient(create_app(settings)) as restarted_client:
        sessions = restarted_client.get("/api/sessions")
        detail = restarted_client.get(f"/api/sessions/{first.json()['id']}")

    assert first.status_code == 201
    assert second.status_code == 201
    assert sessions.status_code == 200
    assert [item["id"] for item in sessions.json()] == [second.json()["id"], first.json()["id"]]
    assert detail.status_code == 200
    assert detail.json()["session"]["title"] == "New session"
    assert detail.json()["messages"] == []
    assert detail.json()["session"]["message_count"] == 0


def test_database_records_the_initial_schema_migration(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)):
        pass

    with closing(sqlite3.connect(settings.database_path)) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert versions == [(1,)]
