from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_provider_config_reads_server_dotenv_and_prefers_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "TRELLIS_OPENAI_API_KEY=file-openai\nTRELLIS_ANTHROPIC_API_KEY=file-anthropic\n"
    )
    monkeypatch.delenv("TRELLIS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ANTHROPIC_API_KEY", raising=False)
    file_settings = Settings(_env_file=dotenv)
    assert isinstance(file_settings.openai_api_key, SecretStr)
    assert isinstance(file_settings.anthropic_api_key, SecretStr)
    assert file_settings.openai_api_key.get_secret_value() == "file-openai"
    assert file_settings.anthropic_api_key.get_secret_value() == "file-anthropic"

    monkeypatch.setenv("TRELLIS_OPENAI_API_KEY", "process-openai")
    monkeypatch.setenv("TRELLIS_ANTHROPIC_API_KEY", "process-anthropic")
    env_settings = Settings(_env_file=dotenv)
    assert isinstance(env_settings.openai_api_key, SecretStr)
    assert isinstance(env_settings.anthropic_api_key, SecretStr)
    assert env_settings.openai_api_key.get_secret_value() == "process-openai"
    assert env_settings.anthropic_api_key.get_secret_value() == "process-anthropic"
    for secret in ("process-openai", "process-anthropic"):
        assert secret not in repr(env_settings)
        assert secret not in env_settings.model_dump_json()
        assert secret not in repr(env_settings.model_dump())


def test_provider_config_has_no_unprefixed_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unprefixed-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unprefixed-anthropic")
    settings = Settings(_env_file=None)
    assert settings.openai_api_key is None
    assert settings.anthropic_api_key is None


def test_provider_config_validation_does_not_expose_invalid_secret_values() -> None:
    with pytest.raises(ValidationError) as failed:
        Settings(_env_file=None, openai_api_key={"secret": "opaque-invalid-key"})
    assert "opaque-invalid-key" not in str(failed.value)


def test_runtime_capabilities_are_chat_only_until_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv("TRELLIS_ENABLED_CAPABILITIES", raising=False)
    assert Settings(_env_file=None).enabled_capabilities == frozenset()
    monkeypatch.setenv("TRELLIS_ENABLED_CAPABILITIES", '["local_read"]')
    assert Settings(_env_file=None).enabled_capabilities == frozenset({"local_read"})


def test_unknown_runtime_capability_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("TRELLIS_ENABLED_CAPABILITIES", '["unrestricted_host"]')
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_settings_load_supabase_values_from_dotenv_and_prefer_process_env(
    monkeypatch, tmp_path: Path
) -> None:
    assert Path(str(Settings.model_config.get("env_file"))) == (
        Path(__file__).resolve().parents[1] / ".env"
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "TRELLIS_SUPABASE_URL=https://file-project.supabase.co\n"
        "TRELLIS_SUPABASE_PUBLISHABLE_KEY=sb_publishable_from_file\n"
    )
    monkeypatch.delenv("TRELLIS_SUPABASE_URL", raising=False)
    monkeypatch.delenv("TRELLIS_SUPABASE_PUBLISHABLE_KEY", raising=False)

    file_settings = Settings(_env_file=dotenv)

    assert file_settings.supabase_url == "https://file-project.supabase.co"
    assert file_settings.supabase_publishable_key == "sb_publishable_from_file"

    monkeypatch.setenv("TRELLIS_SUPABASE_URL", "https://env-project.supabase.co")
    monkeypatch.setenv("TRELLIS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_from_env")

    env_settings = Settings(_env_file=dotenv)

    assert env_settings.supabase_url == "https://env-project.supabase.co"
    assert env_settings.supabase_publishable_key == "sb_publishable_from_env"
