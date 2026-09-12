"""Capture Settings identity and Bolttagu editor from owned Windows Tk HWNDs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tkinter as tk

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from overlay.bolttagu_editor import BolttaguMappingEditor
from overlay.settings_window import _SettingsWindow, settings_product_identity
from scripts.dev.preview_session_stack import capture_window


def widget_texts(widget) -> list[str]:
    values: list[str] = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except (tk.TclError, AttributeError):
            text = ""
        if text:
            values.append(str(text))
        values.extend(widget_texts(child))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    settings = editor = None
    try:
        settings = _SettingsWindow(root)
        root.update()
        identity = settings_product_identity()
        settings_text = widget_texts(settings.window)
        if identity not in settings_text:
            raise AssertionError("settings identity footer missing")
        capture_window(settings.window).save(args.output / "settings.png")

        editor = BolttaguMappingEditor(settings.window, None, args.output / "mappings", lambda _path: None)
        root.update()
        editor_text = widget_texts(editor.window)
        if "적용" not in editor_text or "적용 준비" in editor_text:
            raise AssertionError("mapping action label mismatch")
        height = editor.window.winfo_height()
        apply_widget = next(
            child for child in editor.window.winfo_children()
            if "적용" in widget_texts(child)
        )
        if apply_widget.winfo_rooty() + apply_widget.winfo_height() > editor.window.winfo_rooty() + height:
            raise AssertionError("mapping action row clipped")
        capture_window(editor.window).save(args.output / "mapping-editor.png")
        print(json.dumps({"status": "PASS", "identity": identity,
                          "settings_size": [settings.window.winfo_width(), settings.window.winfo_height()],
                          "editor_size": [editor.window.winfo_width(), height]}, ensure_ascii=False))
        return 0
    finally:
        if editor is not None and editor.window.winfo_exists():
            editor.window.destroy()
        if settings is not None and settings.window.winfo_exists():
            settings.window.destroy()
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
