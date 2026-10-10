from pathlib import Path

from app.core.config import Settings


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
