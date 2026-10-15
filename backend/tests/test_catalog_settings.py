import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.api.routes.settings import serialize_settings
from app.application.errors import ApplicationError
from app.application.settings import SettingsService
from tests.memory_repository import MODELS, MemoryRepository


class Credentials:
    async def status(self, provider: str) -> tuple[bool, str | None]:
        return True, None

    async def get(self, provider: str) -> str | None:
        return "test-key"


def test_settings_exposes_verified_limits_and_disabled_pending_models(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "catalog")
        repository.state.models.append(
            replace(
                MODELS[0], id="openai:unverified", upstream_model_id="unverified", enabled=False
            )
        )
        response = serialize_settings(await SettingsService(repository, Credentials()).get())
        known, _, pending = response.models
        assert known.context_window_tokens == 1_050_000
        assert known.max_output_tokens == 128_000
        assert known.supports_native_compaction is True
        assert known.metadata_status == "ready" and known.selectable is True
        assert known.verification_sources
        assert pending.metadata_status == "pending" and pending.selectable is False
        assert "metadata_unverified" in pending.pending_reasons

    asyncio.run(exercise())


@pytest.mark.parametrize("model_id", ["openai:unknown", "openai:gpt-5.5"])
def test_pending_or_disabled_model_cannot_be_selected(tmp_path: Path, model_id: str) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "catalog")
        repository.state.models = [
            replace(MODELS[0], id=model_id, upstream_model_id=model_id.split(":")[1], enabled=False)
        ]
        with pytest.raises(ApplicationError, match="not available"):
            await SettingsService(repository, Credentials()).select_model(model_id)
        assert repository.state.selected_model_id == "openai:gpt-5.5"

    asyncio.run(exercise())


def test_unknown_enabled_profile_cannot_be_selected(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "catalog")
        repository.state.models = [
            replace(MODELS[0], id="openai:unknown", upstream_model_id="unknown")
        ]
        with pytest.raises(ApplicationError, match="not available"):
            await SettingsService(repository, Credentials()).select_model("openai:unknown")

    asyncio.run(exercise())


@pytest.mark.parametrize("availability", ["unavailable", "unknown"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("selection", ["model", "provider"])
def test_reviewed_model_selection_preserves_choice_despite_availability_and_credentials(
    tmp_path: Path, availability: str, configured: bool, selection: str
) -> None:
    class ProviderCredentials(Credentials):
        async def status(self, provider: str) -> tuple[bool, str | None]:
            return configured, None

    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "catalog")
        repository.state.models = [replace(MODELS[1], availability=availability)]
        service = SettingsService(repository, ProviderCredentials())
        settings = (
            await service.select_model("anthropic:claude-sonnet-5")
            if selection == "model"
            else await service.select_provider("anthropic")
        )
        assert repository.state.selected_model_id == "anthropic:claude-sonnet-5"
        assert settings.selected_provider == "anthropic"
        assert settings.models[0].selectable is True
        assert settings.models[0].configured is configured
        assert settings.models[0].availability == availability

    asyncio.run(exercise())
