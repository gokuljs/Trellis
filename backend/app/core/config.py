from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.capabilities import RuntimeCapability


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRELLIS_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Trellis API"
    environment: str = "development"
    debug: bool = False
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".trellis")
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    web_origin: str | None = None
    enabled_capabilities: frozenset[RuntimeCapability] = frozenset()
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    anthropic_api_key: SecretStr | None = Field(default=None, repr=False)

    durable_execution_enabled: bool = False
    durable_acceptance_enabled: bool = False
    worker_database_url: SecretStr | None = Field(default=None, repr=False)
    worker_capacity: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def validate_durable_mode(self) -> Settings:
        if self.durable_acceptance_enabled and not self.durable_execution_enabled:
            raise ValueError("Durable acceptance requires durable execution mode")
        if self.durable_execution_enabled and self.enabled_capabilities:
            raise ValueError("Durable execution supports chat only")
        return self
