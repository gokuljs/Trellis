"""Test storage must not silently acknowledge a legacy SQLite import."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.infrastructure.sqlite_import import import_locked_account
from tests.memory_repository import MemoryRepository
from tests.test_sqlite_import import _account


@pytest.mark.anyio
async def test_memory_repository_refuses_import_before_completion_marker(tmp_path: Path) -> None:
    user_id = uuid4()
    account_dir, lock_fd = await _account(tmp_path, user_id)
    repository = MemoryRepository(account_dir / "cloud-records", owner_id=user_id)
    marker = account_dir / ".supabase-imported.json"
    try:
        with pytest.raises(RuntimeError, match="in-memory repository cannot import"):
            await import_locked_account(account_dir, user_id, lock_fd, repository)
        assert not marker.exists()
    finally:
        os.close(lock_fd)
