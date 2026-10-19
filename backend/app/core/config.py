from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.origins import canonical_web_origin
from app.domain.capabilities import RuntimeCapability


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRELLIS_",
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    app_name: str = "Trellis API"
    environment: Literal["development", "test", "preview", "production"] = "development"
    debug: bool = False
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".trellis")
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    web_origin: str | None = None
    private_user_ids: frozenset[UUID] = Field(default=frozenset(), repr=False)
    enabled_capabilities: frozenset[RuntimeCapability] = frozenset()
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    anthropic_api_key: SecretStr | None = Field(default=None, repr=False)
    release: str = Field(default="development", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")

    @model_validator(mode="after")
    def validate_hosted_access(self) -> Settings:
        if self.web_origin and not canonical_web_origin(self.web_origin):
            raise ValueError("Web origin must be a canonical HTTPS origin without a path.")
        if self.environment in {"preview", "production"} and (
            not self.web_origin or not self.private_user_ids
        ):
            raise ValueError("Hosted environments require a web origin and private user IDs.")
        return self
