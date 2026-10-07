"""User-managed test commands attached to a canonical workspace."""

import re
from pathlib import Path, PureWindowsPath
from typing import Protocol

from app.application.errors import ApplicationError
from app.application.tools import ToolExecutionError, parse_command, redact_secrets
from app.domain.models import Session, TestPreset

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}\Z")
MAX_TEST_PRESETS = 10


class TestPresetRepository(Protocol):
    async def get_session(self, session_id: str) -> Session | None: ...
    async def list_test_presets(self, workspace_path: str) -> list[TestPreset]: ...
    async def get_test_preset(self, workspace_path: str, name: str) -> TestPreset | None: ...
    async def save_test_preset(self, workspace_path: str, preset: TestPreset) -> None: ...
    async def delete_test_preset(self, workspace_path: str, name: str) -> bool: ...


class TestPresetService:
    def __init__(self, repository: TestPresetRepository) -> None:
        self._repository = repository

    async def _workspace(self, session_id: str) -> str:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise ApplicationError("session_not_found", "Session not found.")
        if session.workspace_path is None:
            raise ApplicationError(
                "workspace_required", "Attach a workspace before saving test commands."
            )
        return session.workspace_path

    async def list_presets(self, session_id: str) -> list[TestPreset]:
        return await self._repository.list_test_presets(await self._workspace(session_id))

    async def save(self, session_id: str, name: str, command: str, cwd: str) -> TestPreset:
        workspace = await self._workspace(session_id)
        if not _NAME.fullmatch(name) or name != name.strip():
            raise ApplicationError("invalid_test_preset", "Enter a short test name.")
        try:
            parse_command(command)
        except ToolExecutionError:
            raise ApplicationError(
                "invalid_test_preset", "Enter one test command without shell operators."
            ) from None
        if redact_secrets(command) != command:
            raise ApplicationError(
                "invalid_test_preset", "Remove credentials from the saved test command."
            )
        if (
            not cwd
            or len(cwd) > 4096
            or "\\" in cwd
            or "\x00" in cwd
            or Path(cwd).is_absolute()
            or PureWindowsPath(cwd).is_absolute()
            or ".." in Path(cwd).parts
        ):
            raise ApplicationError("invalid_test_preset", "Choose a folder inside the workspace.")
        if await self._repository.get_test_preset(workspace, name) is None:
            existing = await self._repository.list_test_presets(workspace)
            if len(existing) >= MAX_TEST_PRESETS:
                raise ApplicationError(
                    "too_many_test_presets", "This workspace has too many saved test commands."
                )
        preset = TestPreset(name, command, cwd)
        await self._repository.save_test_preset(workspace, preset)
        return preset

    async def delete(self, session_id: str, name: str) -> bool:
        return await self._repository.delete_test_preset(await self._workspace(session_id), name)
