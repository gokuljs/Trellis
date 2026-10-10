from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRELLIS_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Trellis API"
    environment: str = "development"
    debug: bool = False
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".trellis")
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    web_origin: str | None = None
