import asyncio

from app.application.errors import ApplicationError
from app.application.ports import WorkspaceDirectoryPicker
from app.domain.capabilities import RuntimeCapabilities

DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS = 15 * 60


class WorkspacePickerService:
    def __init__(
        self,
        picker: WorkspaceDirectoryPicker,
        timeout_seconds: float = DEFAULT_WORKSPACE_PICKER_TIMEOUT_SECONDS,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        self._picker = picker
        self._timeout_seconds = timeout_seconds
        self._capabilities = capabilities or RuntimeCapabilities()

    async def pick_directory(self) -> str | None:
        if not self._capabilities.workspace_enabled:
            raise ApplicationError("capability_disabled", "Local workspace access is disabled.")
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
