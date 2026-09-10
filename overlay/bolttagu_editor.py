"""Native mapping add-on. Explicit import/export; no external application dependency."""
from __future__ import annotations

import json
import tkinter as tk
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from overlay.bolttagu_mapping import OPTIONS, SpriteMap, frames_at, sprite_map, validate_mapping_document
from overlay.native_bolttagu import load_atlas
from core.install.native_bolttagu import store_mapping


def _choice_value(value: object) -> str | None:
    """Canonical single-choice value used by this (single-select) editor."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value if isinstance(value, str) else None


def sparse_mapping_document(
    schema: SpriteMap, document: dict, selections: dict[tuple[str, str], str | None]
) -> dict:
    """Keep valid hidden choices, but publish only overrides of declared defaults.

    ``document`` is always validated before it reaches here.  The editor has no
    control for hidden sections (currently the optional one-shot policy), so
    they must survive a visible-pose edit rather than being accidentally reset.
    """
    result = {"version": 1}
    for section in schema.sections:
        supplied = document.get(section.key, {})
        for row in section.rows:
            if section.hidden:
                value = (
                    _choice_value(supplied[row.key])
                    if row.key in supplied
                    else _choice_value(row.default)
                )
            else:
                value = selections[(section.key, row.key)]
            default = _choice_value(row.default)
            if value != default:
                result.setdefault(section.key, {})[row.key] = value
    validate_mapping_document(result)
    return result


def compose_preview_frame(sheets: dict, option: str, elapsed_ms: int) -> Image.Image:
    """Compose exactly the recipe the native renderer draws for an option."""
    recipe = frames_at(OPTIONS[option], elapsed_ms)
    image = sheets[recipe[0][0]][recipe[0][1]].copy()
    for sheet, cell in recipe[1:]:
        image = Image.alpha_composite(image, sheets[sheet][cell])
    return image


class BolttaguMappingEditor:
    def __init__(self, parent, path: Path | None, directory: Path, on_save):
        self.directory, self.on_save = directory, on_save
        self.schema = sprite_map()
        self.document = {'version': 1}
        if path:
            if path.stat().st_size > 65536:
                raise ValueError('64 KiB 이하 파일만 지원합니다.')
            self.document = json.loads(path.read_text(encoding='utf-8-sig'))
            validate_mapping_document(self.document)
        # All asset/schema/file validation precedes creating any native window.
        self.window = tk.Toplevel(parent)
        self.window.title('볼따구 · 이벤트와 포즈 매핑')
        self.window.geometry('820x760')
        self.variables = {}
        self._preview_after_id: str | None = None
        self._preview_started_ms = int(time.monotonic() * 1000)
        self._preview_option: str | None = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_sheets, self._preview_cell = load_atlas()
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill='both', expand=True, padx=12, pady=12)
        for section in self.schema.sections:
            if section.hidden:
                continue
            frame = ttk.Frame(notebook)
            notebook.add(frame, text={'hints': '상태', 'oneshots': '진입 시 1회', 'categories': '도구 범주', 'lifecycle': '등장 / 퇴장'}[section.key])
            ttk.Label(frame, text=section.note, wraplength=720).grid(row=0, column=0, columnspan=2, sticky='w', padx=8, pady=12)
            for index, row in enumerate(section.rows, start=1):
                ttk.Label(frame, text=f'{row.key} · {row.note}').grid(row=index, column=0, sticky='w', padx=8, pady=4)
                variable = tk.StringVar()
                values = ('(기본값)',) + (('(없음)',) if section.allow_empty else ()) + section.options
                picker = ttk.Combobox(frame, textvariable=variable, values=values, state='readonly', width=30)
                picker.grid(row=index, column=1, sticky='ew', padx=8, pady=4)
                picker.bind('<<ComboboxSelected>>', lambda _event, key=(section.key, row.key): self._select_preview(key))
                self.variables[(section.key, row.key)] = variable
            frame.columnconfigure(1, weight=1)
        preview = ttk.LabelFrame(self.window, text='선택한 동작 미리보기 · packaged native atlas')
        preview.pack(fill='x', padx=12, pady=(0, 12))
        self._preview_caption = tk.StringVar(value='')
        ttk.Label(preview, textvariable=self._preview_caption).pack(anchor='w', padx=8, pady=(6, 0))
        self._preview_canvas = tk.Canvas(
            preview, width=self._preview_cell[0], height=self._preview_cell[1],
            highlightthickness=0, background='#f4f0e8',
        )
        self._preview_canvas.pack(padx=8, pady=8)
        self._preview_image_id = self._preview_canvas.create_image(0, 0, anchor='nw')
        actions = ttk.Frame(self.window)
        actions.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(actions, text='가져오기…', command=self._import).pack(side='left')
        ttk.Button(actions, text='내보내기…', command=self._export).pack(side='left', padx=8)
        ttk.Button(actions, text='모두 기본값', command=self._defaults).pack(side='left')
        ttk.Button(actions, text='적용 준비', command=self._save).pack(side='right')
        ttk.Label(actions, text='설정 창의 저장을 눌러 최종 적용').pack(side='right', padx=8)
        self.window.bind('<Destroy>', self._on_destroy, add='+')
        self._load()

    def _load(self):
        for (section, key), variable in self.variables.items():
            entries = self.document.get(section, {})
            if key not in entries:
                value = '(기본값)'
            else:
                value = entries[key]
                if isinstance(value, list):
                    value = value[0] if value else None
                value = value or '(없음)'
            variable.set(value)
        first = next(iter(self.variables), None)
        if first:
            self._select_preview(first)

    def _collect(self):
        selections = {
            key: None if variable.get() in ('(기본값)', '(없음)') else variable.get()
            for key, variable in self.variables.items()
        }
        # '(기본값)' is not necessarily None: translate it back to each row's
        # declared default before sparse comparison.
        for section in self.schema.sections:
            if section.hidden:
                continue
            for row in section.rows:
                key = (section.key, row.key)
                if self.variables[key].get() == '(기본값)':
                    selections[key] = _choice_value(row.default)
        return sparse_mapping_document(self.schema, self.document, selections)

    def _defaults(self):
        self.document = {'version': 1}
        self._load()

    def _select_preview(self, key: tuple[str, str]) -> None:
        if self._preview_after_id is not None:
            self.window.after_cancel(self._preview_after_id)
            self._preview_after_id = None
        section = next(section for section in self.schema.sections if section.key == key[0])
        row = section.by_key[key[1]]
        value = self.variables[key].get()
        option = _choice_value(row.default) if value == '(기본값)' else (None if value == '(없음)' else value)
        self._preview_option = option
        self._preview_started_ms = int(time.monotonic() * 1000)
        self._preview_caption.set(f'{key[0]}.{key[1]} → {option or "(없음)"}')
        self._render_preview()

    def _render_preview(self) -> None:
        if not self.window.winfo_exists():
            return
        if self._preview_option is None:
            self._preview_canvas.itemconfigure(self._preview_image_id, image='')
        else:
            elapsed = int(time.monotonic() * 1000) - self._preview_started_ms
            image = compose_preview_frame(self._preview_sheets, self._preview_option, elapsed)
            self._preview_photo = ImageTk.PhotoImage(image, master=self._preview_canvas)
            self._preview_canvas.itemconfigure(self._preview_image_id, image=self._preview_photo)
        self._preview_after_id = self.window.after(50, self._render_preview)

    def _on_destroy(self, event) -> None:
        if event.widget is not self.window or self._preview_after_id is None:
            return
        try:
            self.window.after_cancel(self._preview_after_id)
        except tk.TclError:
            pass
        self._preview_after_id = None

    def _import(self):
        path = filedialog.askopenfilename(parent=self.window, filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            if Path(path).stat().st_size > 65536:
                raise ValueError('64 KiB 이하 파일만 지원합니다.')
            document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            validate_mapping_document(document)
            self.document = document
            self._load()
        except (OSError, ValueError) as exc:
            messagebox.showerror('가져오기 실패', str(exc), parent=self.window)

    def _export(self):
        path = filedialog.asksaveasfilename(parent=self.window, defaultextension='.json', filetypes=[('JSON', '*.json')])
        if path:
            try:
                Path(path).write_text(json.dumps(self._collect(), ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, ValueError) as exc:
                messagebox.showerror('내보내기 실패', str(exc), parent=self.window)

    def _save(self):
        try:
            path = store_mapping(self._collect(), self.directory)
            self.on_save(str(path))
            self.window.destroy()
        except (OSError, ValueError) as exc:
            messagebox.showerror('적용 준비 실패', str(exc), parent=self.window)
