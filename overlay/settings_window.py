"""overlay 설정 GUI — tkinter 기반 설정 다이얼로그.

오버레이 우클릭 컨텍스트 메뉴 또는 트레이 아이콘 → '설정'을 누르면 열림.
변경한 값만 ~/.engram/overlay.user.yaml 에 저장한다.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox
from typing import Callable

import yaml

from overlay.config import (
    _ENGRAM_USER_CONFIG_PATH,
    _USER_CONFIG_PATH,
    _safe_load_yaml,
    get_ollama_model,
    load_cfg,
    normalize_cli_provider,
)

_SUPPORTED_PROVIDERS = ["copilot", "gemini", "claude-code", "ollama"]


def _nested_set(d: dict, keys: list[str], value) -> None:
    """중첩 dict에 키 경로로 값을 설정한다."""
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    if value is None or value == "":
        d.pop(keys[-1], None)
    else:
        d[keys[-1]] = value


def _nested_get(d: dict, keys: list[str], default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def open_settings(root: tk.Tk, on_saved: Callable[[], None] | None = None) -> None:
    """설정 창을 열거나 이미 열려 있으면 포커스를 줍니다."""
    # 이미 열린 창이 있으면 포커스 이동
    for widget in root.winfo_children():
        if isinstance(widget, tk.Toplevel) and getattr(widget, "_is_settings_window", False):
            widget.lift()
            widget.focus_force()
            return

    win = _SettingsWindow(root, on_saved=on_saved)
    win.window.focus_force()


class _SettingsWindow:
    def __init__(self, root: tk.Tk, on_saved: Callable[[], None] | None = None):
        self._root = root
        self._on_saved = on_saved

        self.window = tk.Toplevel(root)
        self.window._is_settings_window = True
        self.window.title("Engram 설정")
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)

        # 현재 병합된 설정 + 저장된 사용자 설정 로드
        self._cfg = load_cfg()
        self._user_cfg = _safe_load_yaml(_USER_CONFIG_PATH)
        self._engram_user_cfg = _safe_load_yaml(_ENGRAM_USER_CONFIG_PATH)

        self._build_ui()
        self._load_current_values()
        self._center_window()

    # ──────────────────────────────────────────────────────────── UI 빌드 ──

    def _build_ui(self):
        PAD = {"padx": 8, "pady": 4}

        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self._tab_overlay = ttk.Frame(notebook)
        self._tab_cli = ttk.Frame(notebook)
        self._tab_terminal = ttk.Frame(notebook)

        notebook.add(self._tab_overlay, text="오버레이")
        notebook.add(self._tab_cli, text="CLI 공급자")
        notebook.add(self._tab_terminal, text="터미널")

        self._build_overlay_tab(PAD)
        self._build_cli_tab(PAD)
        self._build_terminal_tab(PAD)

        # 하단 버튼
        btn_frame = ttk.Frame(self.window)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="저장", command=self._save).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="취소", command=self.window.destroy).pack(side="right")

    def _build_overlay_tab(self, PAD: dict):
        f = self._tab_overlay

        # 캐릭터 소스 (파일 또는 폴더 선택)
        ttk.Label(f, text="캐릭터:").grid(row=0, column=0, sticky="w", **PAD)
        self._char_path_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._char_path_var, width=24, state="readonly").grid(row=0, column=1, sticky="ew", **PAD)
        btn_frame_char = ttk.Frame(f)
        btn_frame_char.grid(row=0, column=2, sticky="w", padx=(0, 4), pady=4)
        ttk.Button(btn_frame_char, text="파일...", width=6, command=self._browse_char_file).pack(side="left", padx=(0, 2))
        ttk.Button(btn_frame_char, text="폴더...", width=6, command=self._browse_char_dir).pack(side="left")
        ttk.Label(
            f,
            text="(파일 선택 → 정적 이미지 / 폴더 선택 → 애니메이션)",
            foreground="gray",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        # 캐릭터 높이 비율
        ttk.Label(f, text="캐릭터 높이 비율\n(0.05 ~ 0.5):").grid(row=2, column=0, sticky="w", **PAD)
        self._char_height_var = tk.DoubleVar()
        height_frame = ttk.Frame(f)
        height_frame.grid(row=2, column=1, columnspan=2, sticky="ew", **PAD)
        self._height_scale = ttk.Scale(
            height_frame,
            from_=0.05,
            to=0.5,
            variable=self._char_height_var,
            orient="horizontal",
            length=160,
            command=lambda v: self._height_label.config(text=f"{float(v):.3f}"),
        )
        self._height_scale.pack(side="left")
        self._height_label = ttk.Label(height_frame, text="0.125", width=6)
        self._height_label.pack(side="left", padx=(4, 0))

        # 작업 디렉토리
        ttk.Label(f, text="작업 디렉토리:").grid(row=3, column=0, sticky="w", **PAD)
        self._workdir_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._workdir_var, width=22).grid(row=3, column=1, sticky="ew", **PAD)
        ttk.Button(f, text="찾기...", command=self._browse_workdir).grid(row=3, column=2, **PAD)

        f.columnconfigure(1, weight=1)

    def _build_cli_tab(self, PAD: dict):
        f = self._tab_cli

        # 공급자 선택
        ttk.Label(f, text="기본 공급자:").grid(row=0, column=0, sticky="w", **PAD)
        self._provider_var = tk.StringVar()
        provider_combo = ttk.Combobox(f, textvariable=self._provider_var, values=_SUPPORTED_PROVIDERS, state="readonly", width=18)
        provider_combo.grid(row=0, column=1, sticky="ew", **PAD)

        # Ollama 모델
        ttk.Label(f, text="Ollama 모델:").grid(row=1, column=0, sticky="w", **PAD)
        self._ollama_model_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._ollama_model_var, width=22).grid(row=1, column=1, sticky="ew", **PAD)

        # Ollama 명령어
        ttk.Label(f, text="Ollama 명령어:").grid(row=2, column=0, sticky="w", **PAD)
        self._ollama_cmd_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._ollama_cmd_var, width=22).grid(row=2, column=1, sticky="ew", **PAD)

        # Ollama Base URL
        ttk.Label(f, text="Ollama Base URL:").grid(row=3, column=0, sticky="w", **PAD)
        self._ollama_url_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._ollama_url_var, width=22).grid(row=3, column=1, sticky="ew", **PAD)

        # Gemini 명령어
        ttk.Label(f, text="Gemini 명령어:").grid(row=4, column=0, sticky="w", **PAD)
        self._gemini_cmd_var = tk.StringVar()
        ttk.Entry(f, textvariable=self._gemini_cmd_var, width=22).grid(row=4, column=1, sticky="ew", **PAD)

        f.columnconfigure(1, weight=1)

    def _build_terminal_tab(self, PAD: dict):
        f = self._tab_terminal

        # 폰트 크기
        ttk.Label(f, text="기본 폰트 크기:").grid(row=0, column=0, sticky="w", **PAD)
        self._font_size_var = tk.IntVar()
        ttk.Spinbox(f, textvariable=self._font_size_var, from_=6, to=24, width=8).grid(row=0, column=1, sticky="w", **PAD)

        # 터미널 너비 비율
        ttk.Label(f, text="너비 비율 (0.1~0.8):").grid(row=1, column=0, sticky="w", **PAD)
        self._term_width_var = tk.DoubleVar()
        w_frame = ttk.Frame(f)
        w_frame.grid(row=1, column=1, sticky="ew", **PAD)
        self._width_scale = ttk.Scale(
            w_frame,
            from_=0.1,
            to=0.8,
            variable=self._term_width_var,
            orient="horizontal",
            length=160,
            command=lambda v: self._width_label.config(text=f"{float(v):.2f}"),
        )
        self._width_scale.pack(side="left")
        self._width_label = ttk.Label(w_frame, text="0.20", width=5)
        self._width_label.pack(side="left", padx=(4, 0))

        # 터미널 높이 비율
        ttk.Label(f, text="높이 비율 (0.2~1.0):").grid(row=2, column=0, sticky="w", **PAD)
        self._term_height_var = tk.DoubleVar()
        h_frame = ttk.Frame(f)
        h_frame.grid(row=2, column=1, sticky="ew", **PAD)
        self._theight_scale = ttk.Scale(
            h_frame,
            from_=0.2,
            to=1.0,
            variable=self._term_height_var,
            orient="horizontal",
            length=160,
            command=lambda v: self._theight_label.config(text=f"{float(v):.2f}"),
        )
        self._theight_scale.pack(side="left")
        self._theight_label = ttk.Label(h_frame, text="0.60", width=5)
        self._theight_label.pack(side="left", padx=(4, 0))

        f.columnconfigure(1, weight=1)

    # ─────────────────────────────────────────────── 현재 값 로드 ──

    def _load_current_values(self):
        cfg = self._cfg

        # 오버레이 탭
        char_name = _nested_get(cfg, ["overlay", "character", "name"], "")
        self._char_path_var.set(str(char_name or ""))

        height = _nested_get(cfg, ["overlay", "char_height_ratio"], 0.125)
        self._char_height_var.set(float(height))
        self._height_label.config(text=f"{float(height):.3f}")

        workdir = _nested_get(cfg, ["cli", "workdir"], "")
        if not workdir:
            workdir = str(self._engram_user_cfg.get("workdir") or "")
        self._workdir_var.set(str(workdir))

        # CLI 탭
        provider = _nested_get(cfg, ["cli", "provider"], "copilot")
        self._provider_var.set(normalize_cli_provider(provider))

        ollama_model = get_ollama_model(cfg)
        self._ollama_model_var.set(ollama_model)

        ollama_cmd = _nested_get(cfg, ["cli", "ollama_command"], "ollama")
        self._ollama_cmd_var.set(str(ollama_cmd or "ollama"))

        ollama_url = _nested_get(cfg, ["cli", "ollama_base_url"], "http://localhost:11434")
        self._ollama_url_var.set(str(ollama_url or ""))

        gemini_cmd = _nested_get(cfg, ["cli", "gemini_command"], "gemini")
        self._gemini_cmd_var.set(str(gemini_cmd or "gemini"))

        # 터미널 탭
        font_size = _nested_get(cfg, ["terminal", "base_font_size"], 8)
        self._font_size_var.set(int(font_size))

        t_width = _nested_get(cfg, ["terminal", "width_ratio"], 0.20)
        self._term_width_var.set(float(t_width))
        self._width_label.config(text=f"{float(t_width):.2f}")

        t_height = _nested_get(cfg, ["terminal", "height_ratio"], 0.60)
        self._term_height_var.set(float(t_height))
        self._theight_label.config(text=f"{float(t_height):.2f}")

    # ─────────────────────────────────────────────────── 파일 탐색 ──

    def _browse_char_file(self):
        path = filedialog.askopenfilename(
            parent=self.window,
            title="캐릭터 이미지 선택 (.png)",
            filetypes=[("PNG 이미지", "*.png"), ("모든 파일", "*.*")],
        )
        if path:
            self._char_path_var.set(path)

    def _browse_char_dir(self):
        path = filedialog.askdirectory(
            parent=self.window,
            title="캐릭터 애니메이션 폴더 선택",
        )
        if path:
            self._char_path_var.set(path)

    def _browse_workdir(self):
        path = filedialog.askdirectory(parent=self.window, title="작업 디렉토리 선택")
        if path:
            self._workdir_var.set(path)

    # ──────────────────────────────────────────────────────── 저장 ──

    def _save(self):
        try:
            self._do_save()
            if self._on_saved:
                try:
                    self._on_saved()
                except Exception:
                    pass
            messagebox.showinfo("저장 완료", "설정이 저장되었습니다.\n오버레이를 재시작하면 전체 반영됩니다.", parent=self.window)
            self.window.destroy()
        except Exception as e:
            messagebox.showerror("저장 실패", str(e), parent=self.window)

    def _do_save(self):
        # 기존 user.yaml을 베이스로 사용 (기존 설정 보존)
        user = _safe_load_yaml(_USER_CONFIG_PATH)

        # ── 오버레이 탭 ──
        char_path = self._char_path_var.get().strip()
        _nested_set(user, ["overlay", "character", "name"], char_path or None)

        height = round(self._char_height_var.get(), 3)
        default_height = _nested_get(self._cfg, ["overlay", "char_height_ratio"], 0.125)
        if abs(height - float(default_height)) > 0.001:
            _nested_set(user, ["overlay", "char_height_ratio"], height)

        workdir = self._workdir_var.get().strip()
        _nested_set(user, ["cli", "workdir"], workdir or None)

        # ── CLI 탭 ──
        provider = self._provider_var.get().strip()
        if provider:
            _nested_set(user, ["cli", "provider"], normalize_cli_provider(provider))

        ollama_model = self._ollama_model_var.get().strip()
        _nested_set(user, ["cli", "ollama_model"], ollama_model or None)

        ollama_cmd = self._ollama_cmd_var.get().strip()
        if ollama_cmd and ollama_cmd != "ollama":
            _nested_set(user, ["cli", "ollama_command"], ollama_cmd)

        ollama_url = self._ollama_url_var.get().strip()
        default_url = "http://localhost:11434"
        if ollama_url and ollama_url != default_url:
            _nested_set(user, ["cli", "ollama_base_url"], ollama_url)

        gemini_cmd = self._gemini_cmd_var.get().strip()
        if gemini_cmd and gemini_cmd != "gemini":
            _nested_set(user, ["cli", "gemini_command"], gemini_cmd)

        # ── 터미널 탭 ──
        font_size = int(self._font_size_var.get())
        default_font = _nested_get(self._cfg, ["terminal", "base_font_size"], 8)
        if font_size != int(default_font):
            _nested_set(user, ["terminal", "base_font_size"], font_size)

        t_width = round(self._term_width_var.get(), 2)
        default_tw = _nested_get(self._cfg, ["terminal", "width_ratio"], 0.20)
        if abs(t_width - float(default_tw)) > 0.005:
            _nested_set(user, ["terminal", "width_ratio"], t_width)

        t_height = round(self._term_height_var.get(), 2)
        default_th = _nested_get(self._cfg, ["terminal", "height_ratio"], 0.60)
        if abs(t_height - float(default_th)) > 0.005:
            _nested_set(user, ["terminal", "height_ratio"], t_height)

        # 파일 쓰기
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(
            yaml.safe_dump(user, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ──────────────────────────────────────────── 창 위치 조정 ──

    def _center_window(self):
        self.window.update_idletasks()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        w = self.window.winfo_width()
        h = self.window.winfo_height()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.window.geometry(f"+{x}+{y}")
