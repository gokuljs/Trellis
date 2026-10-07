import asyncio

from app.application.errors import ApplicationError
from app.application.ports import WorkspaceDirectoryPicker

DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS = 15 * 60


class WorkspacePickerService:
    def __init__(
        self,
        picker: WorkspaceDirectoryPicker,
        timeout_seconds: float = DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS,
    ) -> None:
        self._picker = picker
        self._timeout_seconds = timeout_seconds

    async def pick_directory(self) -> str | None:
        try:
            return await asyncio.wait_for(
                self._picker.pick_directory(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            raise ApplicationError(
                "workspace_picker_timeout",
                "The folder picker timed out. Try choosing a folder again.",
            ) from None
        except OSError:
            raise ApplicationError(
                "workspace_picker_unavailable",
                "Trellis could not open the folder picker. Enter a folder path manually.",
            ) from None
