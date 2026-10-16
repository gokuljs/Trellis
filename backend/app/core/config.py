from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
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
        populate_by_name=True,
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
    telemetry_enabled: bool = False
    telemetry_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TRELLIS_TELEMETRY_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
        repr=False,
    )
    telemetry_headers: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TRELLIS_TELEMETRY_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS"),
        repr=False,
    )
    telemetry_service_name: str = Field(
        default="trellis-api",
        validation_alias=AliasChoices("TRELLIS_TELEMETRY_SERVICE_NAME", "OTEL_SERVICE_NAME"),
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$",
    )
    telemetry_sample_ratio: float = Field(default=1.0, ge=0, le=1)
    release: str = Field(default="development", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")

    @model_validator(mode="after")
    def validate_telemetry(self) -> Settings:
        if not self.telemetry_enabled:
            return self
        if not self.telemetry_endpoint or not self.telemetry_headers:
            raise ValueError("Telemetry endpoint and authorization headers are required.")
        try:
            endpoint = urlsplit(self.telemetry_endpoint)
            local_http = self.environment in {"development", "test"} and endpoint.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            valid = (
                endpoint.hostname
                and (endpoint.scheme == "https" or (endpoint.scheme == "http" and local_http))
                and endpoint.port != 0
                and not endpoint.username
                and not endpoint.password
                and not endpoint.query
                and not endpoint.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Telemetry endpoint must be a valid HTTPS OTLP base URL.")
        raw_headers = self.telemetry_headers.get_secret_value()
        if not raw_headers.strip() or any(c in raw_headers for c in "\r\n"):
            raise ValueError("Telemetry authorization headers are invalid.")
        for pair in raw_headers.split(","):
            name, separator, value = pair.partition("=")
            if (
                not separator
                or not name.strip()
                or not value.strip()
                or any(c in unquote(name + value) for c in "\r\n")
            ):
                raise ValueError("Telemetry authorization headers are invalid.")
        return self
