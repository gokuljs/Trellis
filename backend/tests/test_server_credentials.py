import asyncio

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.infrastructure.server_credentials import ServerCredentialStore


def test_server_store_resolves_only_operator_provider_keys() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key=SecretStr("operator-openai"),
        anthropic_api_key=SecretStr("operator-anthropic"),
    )
    credentials = ServerCredentialStore(settings)

    async def exercise() -> None:
        assert await credentials.get("openai") == "operator-openai"
        assert await credentials.get("anthropic") == "operator-anthropic"
        assert await credentials.get("self-hosted") is None
        assert await credentials.status("openai") == (True, None)
        assert await credentials.status("anthropic") == (True, None)
        assert await credentials.status("self-hosted") == (False, None)

    asyncio.run(exercise())
    assert "operator-openai" not in repr(credentials)
    assert "operator-anthropic" not in repr(credentials)


@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_server_store_treats_missing_and_blank_keys_as_unconfigured(value: str | None) -> None:
    credentials = ServerCredentialStore(
        Settings(
            _env_file=None,
            openai_api_key=SecretStr(value) if value is not None else None,
            anthropic_api_key=None,
        )
    )
    assert asyncio.run(credentials.get("openai")) is None
    assert asyncio.run(credentials.status("openai")) == (False, None)


def test_server_store_uses_a_startup_snapshot_for_rotation_and_removal() -> None:
    settings = Settings(
        _env_file=None, openai_api_key=SecretStr(" original-key "), anthropic_api_key=None
    )
    original = ServerCredentialStore(settings)
    settings.openai_api_key = SecretStr("rotated-key")
    rotated = ServerCredentialStore(settings)
    settings.openai_api_key = None
    removed = ServerCredentialStore(settings)

    assert asyncio.run(original.get("openai")) == "original-key"
    assert asyncio.run(rotated.get("openai")) == "rotated-key"
    assert asyncio.run(removed.get("openai")) is None
    assert asyncio.run(removed.status("openai")) == (False, None)
