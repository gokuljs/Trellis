"""Per-process evidence that a user selected a folder on this computer."""

import asyncio
from pathlib import Path


def _still_selected(workspace_root: Path) -> bool:
    try:
        return (
            workspace_root.is_absolute()
            and workspace_root.resolve(strict=True) == workspace_root
            and workspace_root.is_dir()
        )
    except OSError, RuntimeError, ValueError:
        return False


class LocalWorkspaceBindings:
    def __init__(self) -> None:
        self._selected: dict[str, Path] = {}

    def bind(self, session_id: str, workspace_root: Path | None) -> None:
        if workspace_root is None:
            self._selected.pop(session_id, None)
        else:
            self._selected[session_id] = workspace_root

    async def resolve(self, session_id: str, workspace_path: str | None) -> Path | None:
        selected = self._selected.get(session_id)
        if selected is None or workspace_path != str(selected):
            return None
        if not await asyncio.to_thread(_still_selected, selected):
            self._selected.pop(session_id, None)
            return None
        return selected
