import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path


class NativeFolderPicker:
    async def pick_directory(self) -> str | None:
        dialog_script = Path(__file__).with_name("folder_picker_dialog.py")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(dialog_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            raise

        if process.returncode != 0:
            raise OSError("The native folder picker could not start.")

        try:
            selection = json.loads(stdout)
        except UnicodeDecodeError, json.JSONDecodeError:
            raise OSError("The native folder picker returned an invalid response.") from None

        if selection is None:
            return None
        if not isinstance(selection, str) or not selection:
            raise OSError("The native folder picker returned an invalid response.")
        return selection
