import sys
from types import ModuleType

import pytest

from app.infrastructure.folder_picker_dialog import main


class FakeRoot:
    def __init__(self) -> None:
        self.hidden = False
        self.destroyed = False

    def withdraw(self) -> None:
        self.hidden = True

    def destroy(self) -> None:
        self.destroyed = True


@pytest.mark.parametrize(
    ("selection", "expected"),
    [("/tmp/workspace", '"/tmp/workspace"\n'), ("", "null\n")],
)
def test_folder_picker_dialog_serializes_selection_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selection: str,
    expected: str,
) -> None:
    root = FakeRoot()
    tk_module = ModuleType("tkinter")
    filedialog_module = ModuleType("tkinter.filedialog")
    tk_module.__dict__["Tk"] = lambda: root
    tk_module.__dict__["filedialog"] = filedialog_module
    filedialog_module.__dict__["askdirectory"] = lambda **_kwargs: selection
    monkeypatch.setitem(sys.modules, "tkinter", tk_module)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog_module)

    main()

    assert capsys.readouterr().out == expected
    assert root.hidden
    assert root.destroyed
