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
    enabled_capabilities: frozenset[RuntimeCapability] = frozenset(
        {"web_search", "web_read", "web_crawl"}
    )
    max_active_runs: int = Field(default=16, ge=1, le=256)
    max_active_runs_per_user: int = Field(default=4, ge=1, le=256)
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    anthropic_api_key: SecretStr | None = Field(default=None, repr=False)
    brave_search_api_key: SecretStr | None = Field(default=None, repr=False)
    brave_search_monthly_limit: int = Field(default=1000, ge=1, le=1000)
    web_worker_image: str | None = None
    web_gateway_image: str | None = None
    web_gateway_container: str = "trellis-public-egress"
    web_gateway_ipc_volume: str = "trellis-public-egress"
    web_gateway_secret_file: Path | None = None
    web_gateway_control_url: str = "http://127.0.0.1:8081"
    web_gateway_admin_secret: SecretStr | None = Field(default=None, repr=False)
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
