from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRELLIS_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Trellis API"
    environment: str = "development"
    debug: bool = False
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".trellis")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / ".env"
