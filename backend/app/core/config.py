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
