import asyncio
from pathlib import Path

from app.core.config import Settings
from tests.test_account_registry import ALICE, BOB, AccountRegistry


def test_account_context_ignores_old_key_files_and_uses_server_keys(tmp_path: Path) -> None:
    account_dir = tmp_path / "accounts" / str(ALICE)
    account_dir.mkdir(parents=True, mode=0o700)
    account_dir.parent.chmod(0o700)
    old_file = account_dir / ".env"
    original = "OPENAI_API_KEY=old-private-key\nANTHROPIC_API_KEY=old-other-key\n"
    old_file.write_text(original)
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        openai_api_key="operator-current-key",
        anthropic_api_key=None,
    )

    async def exercise() -> None:
        registry = AccountRegistry(settings)
        try:
            alice = await registry.get(ALICE)
            bob = await registry.get(BOB)
            assert await alice.secret_store.get("openai") == "operator-current-key"
            assert await bob.secret_store.get("openai") == "operator-current-key"
            assert await alice.secret_store.get("anthropic") is None
            assert await alice.secret_store.status("openai") == (True, None)
            assert not hasattr(alice.secret_store, "set")
            assert not hasattr(alice.secret_store, "delete")
        finally:
            await registry.close()

    asyncio.run(exercise())
    assert old_file.read_text() == original
    assert not (tmp_path / "accounts" / str(BOB) / ".env").exists()
