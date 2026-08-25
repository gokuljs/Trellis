from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app, create_app


def test_settings_use_safe_development_defaults(monkeypatch) -> None:
    monkeypatch.delenv("TRELLIS_APP_NAME", raising=False)
    monkeypatch.delenv("TRELLIS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("TRELLIS_DEBUG", raising=False)

    settings = Settings()

    assert settings.app_name == "Trellis API"
    assert settings.environment == "development"
    assert settings.debug is False


def test_settings_read_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("TRELLIS_APP_NAME", "Trellis Test")
    monkeypatch.setenv("TRELLIS_ENVIRONMENT", "test")
    monkeypatch.setenv("TRELLIS_DEBUG", "true")

    settings = Settings()

    assert settings.app_name == "Trellis Test"
    assert settings.environment == "test"
    assert settings.debug is True


def test_app_factory_accepts_injected_settings() -> None:
    settings = Settings(app_name="Injected Trellis", environment="test", debug=True)

    configured_app = create_app(settings)

    assert configured_app.title == "Injected Trellis"
    assert configured_app.debug is True


def test_health_returns_typed_status() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_health_is_documented_in_openapi() -> None:
    schema = app.openapi()

    assert "/health" in schema["paths"]
    assert schema["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"]
