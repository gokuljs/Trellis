import json


def main() -> None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        selection = filedialog.askdirectory(
            parent=root,
            title="Choose a workspace folder",
            mustexist=True,
        )
        print(json.dumps(selection or None))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
