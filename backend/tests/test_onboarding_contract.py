"""Cloud account onboarding rejects out-of-order and conflicting updates."""

from pathlib import Path

import pytest

from app.core.config import Settings
from tests.memory_repository import MemoryRepository
from tests.support import TestClient, account_context, create_app


def test_onboarding_rejects_invalid_order_and_unavailable_model(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))
    with TestClient(app) as client:
        before_intro = client.put("/api/onboarding/steps/profile", json={"display_name": "Alice"})
        assert before_intro.status_code == 409
        assert before_intro.json()["error"]["code"] == "onboarding_step_out_of_order"

        assert client.put("/api/onboarding/steps/intro").status_code == 200
        blank_name = client.put("/api/onboarding/steps/profile", json={"display_name": "   "})
        assert blank_name.status_code == 422
        assert blank_name.json()["error"]["code"] == "invalid_profile"

        before_profile = client.put(
            "/api/onboarding/steps/model", json={"model_id": "openai:gpt-5.5"}
        )
        assert before_profile.status_code == 409
        assert before_profile.json()["error"]["code"] == "onboarding_step_out_of_order"

        assert (
            client.put("/api/onboarding/steps/profile", json={"display_name": "Alice"}).status_code
            == 200
        )
        unknown_model = client.put(
            "/api/onboarding/steps/model", json={"model_id": "unknown:model"}
        )
        assert unknown_model.status_code == 404
        assert unknown_model.json()["error"]["code"] == "model_not_available"
        assert client.get("/api/onboarding").json()["current_step"] == "model"


def test_completed_onboarding_is_idempotent_only_for_selected_model(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.put("/api/onboarding/steps/intro").status_code == 200
        assert (
            client.put("/api/onboarding/steps/profile", json={"display_name": "Alice"}).status_code
            == 200
        )

        oversized_key = client.put(
            "/api/onboarding/steps/model",
            json={"model_id": "openai:gpt-5.5", "api_key": "x" * 10_001},
        )
        assert oversized_key.status_code == 422
        assert oversized_key.json()["error"]["code"] == "invalid_api_key"

        selected = client.put(
            "/api/onboarding/steps/model",
            json={"model_id": "openai:gpt-5.5", "api_key": "sk-test-secret"},
        )
        repeated = client.put("/api/onboarding/steps/model", json={"model_id": "openai:gpt-5.5"})
        changed = client.put(
            "/api/onboarding/steps/model", json={"model_id": "anthropic:claude-sonnet-5"}
        )

    assert selected.status_code == 200
    assert selected.json() == {"current_step": "complete", "completed": True}
    assert repeated.status_code == 200
    assert repeated.json() == selected.json()
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "onboarding_already_complete"


def test_onboarding_reports_a_lost_model_commit_without_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.put("/api/onboarding/steps/intro").status_code == 200
        assert (
            client.put("/api/onboarding/steps/profile", json={"display_name": "Alice"}).status_code
            == 200
        )
        assert (
            client.put(
                "/api/settings/providers/openai/api-key", json={"api_key": "sk-test-secret"}
            ).status_code
            == 200
        )
        repository = account_context(app).database
        assert isinstance(repository, MemoryRepository)

        async def lost_commit(_model_id: str) -> bool:
            return False

        monkeypatch.setattr(repository, "complete_onboarding", lost_commit)
        failed = client.put("/api/onboarding/steps/model", json={"model_id": "openai:gpt-5.5"})
        progress = client.get("/api/onboarding")

    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "onboarding_step_out_of_order"
    assert progress.json() == {"current_step": "model", "completed": False}
