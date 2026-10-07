import asyncio
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.runtime import RunEventType, RunStatus
from app.infrastructure.database import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_VERSION,
    Database,
)
from app.infrastructure.secrets import SecretStore
from app.main import create_app


def make_settings(data_dir: Path) -> Settings:
    return Settings(environment="test", data_dir=data_dir)


EXPECTED_SCHEMA_VERSIONS = [(version,) for version in range(1, SCHEMA_VERSION + 1)]


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


def test_application_startup_marks_a_persisted_active_run_interrupted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)

    async def create_run() -> str:
        await database.initialize()
        session = await database.create_session()
        run = await database.create_run(
            session.id,
            "restart-turn",
            "restart-request",
            "Resume after restart",
            (await database.list_models())[0],
        )
        return run.id

    run_id = asyncio.run(create_run())
    with TestClient(create_app(settings)):
        run = asyncio.run(database.get_run(run_id))
        events = asyncio.run(database.list_run_events(run_id))

    assert run is not None
    assert run.status is RunStatus.INTERRUPTED
    assert run.recovery_count == 1
    assert events[-1].event_type is RunEventType.INTERRUPTED


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


def test_rejected_api_key_is_not_disclosed_in_validation_response(tmp_path: Path) -> None:
    rejected_secret = "sk-" + ("private" * 2_000)

    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": rejected_secret},
        )

    assert response.status_code == 422
    assert rejected_secret not in response.text


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


def test_model_catalog_uses_opaque_ids_and_selection_survives_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        initial = client.get("/api/settings")
        catalog = initial.json()["models"]
        assert {model["provider_id"] for model in catalog} == {"openai", "anthropic"}
        selected_model = next(model for model in catalog if model["provider_id"] == "anthropic")
        response = client.put("/api/settings/model", json={"model_id": selected_model["id"]})

    with TestClient(create_app(settings)) as restarted_client:
        restored = restarted_client.get("/api/settings").json()

    assert response.status_code == 200
    assert response.json()["selected_model_id"] == selected_model["id"]
    assert restored["selected_model_id"] == selected_model["id"]
    assert restored["selected_provider"] == "anthropic"


def test_model_selection_rejects_an_unregistered_model_id(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.put("/api/settings/model", json={"model_id": "deepseek:chat"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_available"


def test_default_run_budget_can_be_changed_and_survives_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        initial = client.get("/api/settings")
        changed = client.put("/api/settings/budget", json={"budget_preset": "longer"})
        invalid = client.put("/api/settings/budget", json={"budget_preset": "unlimited"})

    with TestClient(create_app(settings)) as restarted_client:
        restored = restarted_client.get("/api/settings")

    assert initial.json()["default_budget_preset"] == "conservative"
    assert changed.status_code == 200
    assert changed.json()["default_budget_preset"] == "longer"
    assert restored.json()["default_budget_preset"] == "longer"
    assert invalid.status_code == 422


def test_onboarding_progress_and_answers_are_saved_after_each_step(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        initial = client.get("/api/onboarding")
        intro = client.put("/api/onboarding/steps/intro")
        profile = client.put(
            "/api/onboarding/steps/profile",
            json={"display_name": "Ada", "email": "ada@example.com"},
        )
        repeated_intro = client.put("/api/onboarding/steps/intro")

    with TestClient(create_app(settings)) as restarted_client:
        resumed = restarted_client.get("/api/onboarding")
        restored_profile = restarted_client.get("/api/profile")
        model = next(
            item
            for item in restarted_client.get("/api/settings").json()["models"]
            if item["provider_id"] == "openai"
        )
        completed = restarted_client.put(
            "/api/onboarding/steps/model",
            json={"model_id": model["id"], "api_key": "sk-onboarding-secret"},
        )
        final_state = restarted_client.get("/api/onboarding")

    assert initial.json() == {"current_step": "intro", "completed": False}
    assert intro.json() == {"current_step": "profile", "completed": False}
    assert profile.json() == {"current_step": "model", "completed": False}
    assert repeated_intro.json() == {"current_step": "model", "completed": False}
    assert resumed.json() == {"current_step": "model", "completed": False}
    assert restored_profile.json()["display_name"] == "Ada"
    assert completed.status_code == 200
    assert final_state.json() == {"current_step": "complete", "completed": True}
    assert b"sk-onboarding-secret" not in settings.database_path.read_bytes()


def test_onboarding_model_step_requires_credentials_without_advancing(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.put("/api/onboarding/steps/intro")
        client.put(
            "/api/onboarding/steps/profile",
            json={"display_name": "Ada", "email": "ada@example.com"},
        )
        model = client.get("/api/settings").json()["models"][0]
        response = client.put(
            "/api/onboarding/steps/model",
            json={"model_id": model["id"], "api_key": None},
        )
        state = client.get("/api/onboarding")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert state.json() == {"current_step": "model", "completed": False}


def test_existing_installations_with_a_profile_are_backfilled_as_complete(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript(SCHEMA_V1)
        connection.executescript(SCHEMA_V2)
        connection.executescript(SCHEMA_V3)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, "v1"), (2, "v2"), (3, "v3")],
        )
        connection.execute(
            """INSERT INTO users(id, display_name, email, created_at, updated_at)
               VALUES ('existing-installation', 'Ada', 'ada@example.com', 'v3', 'v3')"""
        )
        connection.execute(
            """INSERT INTO app_settings(id, selected_provider, selected_model_id, updated_at)
               VALUES (1, 'openai', 'openai:gpt-5.5', 'v3')"""
        )
        connection.commit()

    asyncio.run(Database(settings.database_path).initialize())

    with TestClient(create_app(settings)) as client:
        state = client.get("/api/onboarding")

    assert state.json() == {"current_step": "complete", "completed": True}


def test_concurrent_secret_updates_preserve_both_provider_keys(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first_store = SecretStore(settings.secrets_path)
    second_store = SecretStore(settings.secrets_path)

    async def update_both() -> tuple[str | None, str | None]:
        await asyncio.gather(
            first_store.set("openai", "sk-openai-concurrent"),
            second_store.set("anthropic", "sk-anthropic-concurrent"),
        )
        return await first_store.get("openai"), await second_store.get("anthropic")

    assert asyncio.run(update_both()) == (
        "sk-openai-concurrent",
        "sk-anthropic-concurrent",
    )
    assert not list(tmp_path.glob(".env.*.tmp"))


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


def test_database_records_all_schema_migrations(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with TestClient(create_app(settings)):
        pass

    with closing(sqlite3.connect(settings.database_path)) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert versions == EXPECTED_SCHEMA_VERSIONS


def test_database_upgrades_an_existing_v1_schema(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.executescript(SCHEMA_V1)
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'v1')")
        connection.commit()

    asyncio.run(Database(settings.database_path).initialize())

    with closing(sqlite3.connect(settings.database_path)) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        claim_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'turn_claims'"
        ).fetchone()
        default_budget = connection.execute(
            "SELECT default_budget_preset FROM app_settings WHERE id = 1"
        ).fetchone()

    assert versions == EXPECTED_SCHEMA_VERSIONS
    assert claim_table == ("turn_claims",)
    assert default_budget == ("conservative",)


def test_database_upgrades_v5_runs_without_changing_public_messages(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5):
            connection.executescript(schema)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
            [(1,), (2,), (3,), (4,), (5,)],
        )
        connection.execute(
            """INSERT INTO users(id, created_at, updated_at)
               VALUES ('user-1', 'old', 'old')"""
        )
        connection.execute(
            """INSERT INTO sessions(id, user_id, title, created_at, updated_at)
               VALUES ('session-1', 'user-1', 'Old chat', 'old', 'old')"""
        )
        connection.execute(
            """INSERT INTO messages(
                   id, session_id, turn_id, ordinal, role, content, created_at
               ) VALUES ('message-1', 'session-1', 'turn-1', 1, 'user', 'Old request', 'old')"""
        )
        connection.execute(
            """INSERT INTO runs(
                   id, session_id, turn_id, input_message_id, client_request_id, status,
                   provider_id, model_id, adapter_kind, upstream_model_id, max_model_calls,
                   max_tool_calls, deadline_at, created_at
               ) VALUES (
                   'run-1', 'session-1', 'turn-1', 'message-1', 'request-1', 'completed',
                   'openai', 'openai:gpt-5.5', 'openai', 'gpt-5.5', 1, 0, 'old', 'old'
               )"""
        )
        connection.commit()

    database = Database(settings.database_path)
    asyncio.run(database.initialize())
    messages = asyncio.run(database.list_messages("session-1"))
    run = asyncio.run(database.get_run("run-1"))
    with closing(sqlite3.connect(settings.database_path)) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        projection_tables = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE name IN ('run_messages', 'tool_calls') ORDER BY name"""
        ).fetchall()

    assert versions == EXPECTED_SCHEMA_VERSIONS
    assert [message.content for message in messages] == ["Old request"]
    assert run is not None and run.status is RunStatus.COMPLETED
    assert run.budget_preset == "legacy"
    assert (run.max_model_calls, run.max_tool_calls) == (1, 0)
    assert foreign_key_errors == []
    assert projection_tables == [("run_messages",), ("tool_calls",)]


def test_database_rejects_a_schema_from_a_newer_trellis_version(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    asyncio.run(Database(settings.database_path).initialize())
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'future')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="newer Trellis version"):
        asyncio.run(Database(settings.database_path).initialize())
