import asyncio
import json
import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to the in-process lock.
    fcntl = None  # type: ignore[assignment]

from app.domain.models import ProviderName

ENV_NAMES: dict[ProviderName, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def get(self, provider: ProviderName) -> str | None:
        values = await asyncio.to_thread(dotenv_values, self.path)
        value = values.get(ENV_NAMES[provider])
        return value if isinstance(value, str) and value else None

    async def set(self, provider: ProviderName, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update, provider, value)

    async def delete(self, provider: ProviderName) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update, provider, None)

    async def status(self, provider: ProviderName) -> tuple[bool, str | None]:
        value = await self.get(provider)
        if value is None:
            return False, None
        return True, f"••••{value[-4:]}"

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)

    def _update(self, provider: ProviderName, value: str | None) -> None:
        self._prepare_path()
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(lock_descriptor, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            if not self.path.exists() and value is None:
                return
            current = dotenv_values(self.path)
            secrets = {
                name: stored
                for name in ENV_NAMES.values()
                if isinstance((stored := current.get(name)), str) and stored
            }
            env_name = ENV_NAMES[provider]
            if value is None:
                secrets.pop(env_name, None)
            else:
                secrets[env_name] = value
            self._atomic_write(secrets)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def _atomic_write(self, secrets: dict[str, str]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f"{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.fchmod(temporary.fileno(), 0o600)
                for name in ENV_NAMES.values():
                    if name in secrets:
                        temporary.write(f"{name}={json.dumps(secrets[name])}\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
            self._sync_parent_directory()
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def _sync_parent_directory(self) -> None:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
        except OSError:  # pragma: no cover - not all platforms allow directory handles.
            return
        try:
            os.fsync(descriptor)
        except OSError:  # pragma: no cover - durability hint unsupported by the filesystem.
            pass
        finally:
            os.close(descriptor)
