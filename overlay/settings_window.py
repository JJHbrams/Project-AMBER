"""overlay 설정 GUI — tkinter 기반 설정 다이얼로그.

오버레이 우클릭 컨텍스트 메뉴 또는 트레이 아이콘 → '설정'을 누르면 열림.
변경한 값만 ~/.engram/overlay.user.yaml 에 저장한다.
"""

from __future__ import annotations

import os
import subprocess
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
_USER_PERSONA_PATH = Path.home() / ".engram" / "persona.user.yaml"
_PROJECT_PERSONA_PATH = Path(__file__).parent.parent / "config" / "persona.yaml"
_PERSONA_NUMERIC_FIELDS = ("warmth", "formality", "humor", "directness")
_PERSONA_DEFAULTS = {
    "warmth": 0.5,
    "formality": 0.5,
    "humor": 0.3,
    "directness": 0.5,
}

# ── Autostart (Startup 폴더 .lnk) ───────────────────────────────────────
_STARTUP_DIR = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
_STARTUP_LINK = _STARTUP_DIR / "engram-overlay.lnk"
_OVERLAY_CMD = Path.home() / ".engram" / "engram-overlay.cmd"
_OVERLAY_EXE: Path | None = None  # resolved lazily


def _resolve_overlay_target() -> Path | None:
    if _OVERLAY_CMD.exists():
        return _OVERLAY_CMD
    global _OVERLAY_EXE
    if _OVERLAY_EXE and _OVERLAY_EXE.exists():
        return _OVERLAY_EXE
    return None


def _is_autostart_enabled() -> bool:
    return _STARTUP_LINK.exists()


def _set_autostart(enabled: bool) -> None:
    if enabled:
        target = _resolve_overlay_target()
        if target is None:
            raise RuntimeError(
                "engram-overlay.cmd 를 찾을 수 없습니다.\n"
                ".engram/ 폴더를 확인하세요."
            )
        _STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        ps = (
            f'$s = New-Object -ComObject WScript.Shell; '
            f'$sc = $s.CreateShortcut(\'{ _STARTUP_LINK }\'); '
            f'$sc.TargetPath = \'{ target }\'; '
            f'$sc.WorkingDirectory = \'{ target.parent }\'; '
            f'$sc.Description = \'Engram Overlay \u2014 Auto Start\'; '
            f'$sc.Save()'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            _STARTUP_LINK.unlink()
        except FileNotFoundError:
            pass

_PERSONA_USER_TEMPLATE = """# engram persona 사용자 오버라이드
# 이 파일은 "사용자 고정값(pinned)" 오버라이드입니다.
# 값이 있는 필드는 DB 진화값보다 항상 우선 적용됩니다.
#
# 원하는 페르소나를 "항상 유지"하려면 아래 모든 필드에 값을 채우세요.
# 일부 필드만 고정하고 싶다면 원하는 필드만 채우고 나머지는 비워두세요.

# voice: ""
# traits: []
# quirks: []
# values: []
# warmth: 0.50
# formality: 0.50
# humor: 0.30
# directness: 0.50
"""


def _coerce_persona_number(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 2)


def _coerce_persona_list(value) -> list[str]:
    if isinstance(value, list):
        out = []
        for item in value:
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_csv_field(raw: str) -> list[str]:
    values = [token.strip() for token in str(raw or "").replace("，", ",").split(",")]
    return [token for token in values if token]


def _persona_has_custom_override(persona: dict | None) -> bool:
    if not isinstance(persona, dict):
        return False

    voice = persona.get("voice")
    if isinstance(voice, str) and voice.strip():
        return True

    for key in ("traits", "quirks", "values"):
        if _coerce_persona_list(persona.get(key)):
            return True

    fewshot = persona.get("fewshot")
    if isinstance(fewshot, str) and fewshot.strip():
        return True

    for key in _PERSONA_NUMERIC_FIELDS:
        if isinstance(persona.get(key), (int, float)):
            return True

    return False


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
        self._toast_after_id: str | None = None

        self.window = tk.Toplevel(root)
        self.window._is_settings_window = True
        self.window.title("Engram 설정")
        self.window.resizable(True, True)
        self.window.attributes("-topmost", True)

        # 현재 병합된 설정 + 저장된 사용자 설정 로드
        self._cfg = load_cfg()
        self._user_cfg = _safe_load_yaml(_USER_CONFIG_PATH)
        self._engram_user_cfg = _safe_load_yaml(_ENGRAM_USER_CONFIG_PATH)
        self._persona_voice_txt: tk.Text | None = None
        self._persona_traits_txt: tk.Text | None = None
        self._persona_quirks_txt: tk.Text | None = None
        self._persona_values_txt: tk.Text | None = None
        self._persona_fewshot_txt: tk.Text | None = None
        self._persona_numeric_vars: dict[str, tk.DoubleVar] = {}
        self._persona_numeric_pin_vars: dict[str, tk.BooleanVar] = {}
        self._persona_numeric_label_vars: dict[str, tk.StringVar] = {}
        self._persona_banner_var = tk.StringVar(value="현재 기본 페르소나가 적용되어 있습니다. 커스텀 페르소나를 적용해 보세요.")
        self._autostart_var = tk.BooleanVar()
        self._auto_inject_var = tk.BooleanVar()

        self._build_ui()
        self._load_current_values()
        self._center_window()

    # ──────────────────────────────────────────────────────────── UI 빌드 ──

    def _build_ui(self):
        PAD = {"padx": 8, "pady": 4}

        tip_frame = tk.Frame(self.window, bd=1, relief="solid", bg="#f4f6e1")
        tip_frame.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(
            tip_frame,
            textvariable=self._persona_banner_var,
            bg="#f4f6e1",
            anchor="w",
            justify="left",
        ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        ttk.Button(tip_frame, text="페르소나 열기", command=self._open_persona_file).pack(side="right", padx=6, pady=4)

        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=(8, 10))

        self._tab_overlay = ttk.Frame(notebook)
        self._tab_cli = ttk.Frame(notebook)
        self._tab_persona = ttk.Frame(notebook)
        self._tab_terminal = ttk.Frame(notebook)
        self._tab_global = ttk.Frame(notebook)

        notebook.add(self._tab_overlay, text="오버레이")
        notebook.add(self._tab_cli, text="CLI 공급자")
        notebook.add(self._tab_persona, text="페르소나")
        notebook.add(self._tab_terminal, text="터미널")
        notebook.add(self._tab_global, text="전역")

        self._build_overlay_tab(PAD)
        self._build_cli_tab(PAD)
        self._build_persona_tab(PAD)
        self._build_terminal_tab(PAD)
        self._build_global_tab(PAD)

        self._save_feedback_var = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self._save_feedback_var, foreground="gray").pack(fill="x", padx=12, pady=(0, 4))

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

        ttk.Label(
            f,
            text="힌트: persona.user.yaml에 작성한 값은 자동 진화보다 우선 적용됩니다.",
            foreground="gray",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
        ttk.Button(f, text="페르소나 파일 열기", command=self._open_persona_file).grid(row=6, column=0, columnspan=2, sticky="e", padx=8, pady=(0, 6))

        f.columnconfigure(1, weight=1)

    @staticmethod
    def _make_resizable_text(parent, height: int = 3):
        """word-wrap 멀티라인 Text + 하단 드래그 리사이즈 그립.
        Returns (outer_frame, tk.Text).
        """
        outer = tk.Frame(parent)
        txt = tk.Text(
            outer,
            height=height,
            wrap="word",
            relief="sunken",
            bd=1,
            font=("TkDefaultFont", 9),
        )
        txt.pack(fill="both", expand=True)

        grip = tk.Frame(outer, height=6, cursor="sb_v_double_arrow", bg="#e0e0e0")
        grip.pack(fill="x", side="bottom")
        tk.Label(
            grip, text="·  ·  ·", bg="#e0e0e0", foreground="#a8a8a8",
            font=("TkDefaultFont", 7), anchor="e",
        ).place(relx=1.0, rely=0.5, anchor="e", x=-4)

        grip._drag_y = 0  # type: ignore[attr-defined]
        grip._txt = txt    # type: ignore[attr-defined]

        def _press(e):
            grip._drag_y = e.y_root  # type: ignore[attr-defined]

        def _drag(e):
            dy = e.y_root - grip._drag_y  # type: ignore[attr-defined]
            if abs(dy) < 3:
                return
            t = grip._txt  # type: ignore[attr-defined]
            cur_h = int(t.cget("height"))
            px_per_line = t.winfo_height() / max(cur_h, 1)
            delta = round(dy / max(px_per_line, 4))
            if delta == 0:
                return
            t.config(height=max(2, cur_h + delta))
            grip._drag_y = e.y_root  # type: ignore[attr-defined]

        grip.bind("<ButtonPress-1>", _press)
        grip.bind("<B1-Motion>", _drag)
        return outer, txt

    def _build_persona_tab(self, PAD: dict):
        f = self._tab_persona

        ttk.Label(
            f,
            text="말투/가치는 직접 입력하고, 숫자 슬라이더는 pin 체크로 고정 여부를 선택하세요.",
            foreground="gray",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 2))

        ttk.Label(f, text="voice:").grid(row=1, column=0, sticky="nw", **PAD)
        voice_fr, self._persona_voice_txt = self._make_resizable_text(f, height=3)
        voice_fr.grid(row=1, column=1, columnspan=3, sticky="ew", **PAD)

        ttk.Label(f, text="traits\n(쉼표 구분):").grid(row=2, column=0, sticky="nw", **PAD)
        traits_fr, self._persona_traits_txt = self._make_resizable_text(f, height=2)
        traits_fr.grid(row=2, column=1, columnspan=3, sticky="ew", **PAD)

        ttk.Label(f, text="quirks\n(쉼표 구분):").grid(row=3, column=0, sticky="nw", **PAD)
        quirks_fr, self._persona_quirks_txt = self._make_resizable_text(f, height=2)
        quirks_fr.grid(row=3, column=1, columnspan=3, sticky="ew", **PAD)

        ttk.Label(f, text="values\n(쉼표 구분):").grid(row=4, column=0, sticky="nw", **PAD)
        values_fr, self._persona_values_txt = self._make_resizable_text(f, height=2)
        values_fr.grid(row=4, column=1, columnspan=3, sticky="ew", **PAD)

        ttk.Separator(f, orient="horizontal").grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=(6, 2))

        ttk.Label(f, text="말투 예시\n(few-shot):").grid(row=6, column=0, sticky="nw", **PAD)
        fewshot_fr, self._persona_fewshot_txt = self._make_resizable_text(f, height=4)
        fewshot_fr.grid(row=6, column=1, columnspan=3, sticky="ew", **PAD)
        ttk.Label(
            f,
            text="응답 예시를 자유롭게 입력하세요.\n예) user: 오늘 배포 어때?  →  assistant: 됐음. 근데 테스트가 좀 걸려.",
            foreground="gray",
        ).grid(row=7, column=0, columnspan=4, sticky="w", padx=16, pady=(0, 6))

        ttk.Separator(f, orient="horizontal").grid(row=8, column=0, columnspan=4, sticky="ew", padx=8, pady=(6, 2))
        ttk.Label(f, text="Adaptive Slider", foreground="gray").grid(row=9, column=0, sticky="w", padx=8, pady=(2, 0))
        ttk.Label(f, text="값", foreground="gray").grid(row=9, column=2, sticky="w", padx=(0, 6), pady=(2, 0))
        ttk.Label(f, text="pin", foreground="gray").grid(row=9, column=3, sticky="w", padx=(0, 8), pady=(2, 0))

        for idx, field in enumerate(_PERSONA_NUMERIC_FIELDS):
            row = 10 + idx
            value_var = tk.DoubleVar(value=_PERSONA_DEFAULTS[field])
            pin_var = tk.BooleanVar(value=False)
            label_var = tk.StringVar(value=f"{_PERSONA_DEFAULTS[field]:.2f}")

            self._persona_numeric_vars[field] = value_var
            self._persona_numeric_pin_vars[field] = pin_var
            self._persona_numeric_label_vars[field] = label_var

            ttk.Label(f, text=f"{field}:").grid(row=row, column=0, sticky="w", **PAD)
            ttk.Scale(
                f,
                from_=0.0,
                to=1.0,
                variable=value_var,
                orient="horizontal",
                length=190,
                command=lambda raw, key=field: self._on_persona_slider_changed(key, raw),
            ).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
            ttk.Label(f, textvariable=label_var, width=5).grid(row=row, column=2, sticky="w", padx=(0, 6), pady=4)
            ttk.Checkbutton(f, variable=pin_var).grid(row=row, column=3, sticky="w", padx=(0, 8), pady=4)

        ttk.Label(
            f,
            text="pin 해제된 슬라이더는 persona.user.yaml에서 주석(adaptive)으로 기록되어 DB 학습을 허용합니다.",
            foreground="gray",
        ).grid(row=14, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 8))

        f.columnconfigure(1, weight=1)

    def _on_persona_slider_changed(self, field: str, raw_value):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = float(self._persona_numeric_vars[field].get())
        self._persona_numeric_label_vars[field].set(f"{value:.2f}")

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

    def _build_global_tab(self, PAD: dict):
        f = self._tab_global

        # ── 자동 시작 ──
        ttk.Label(f, text="시스템 설정", font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 2)
        )
        ttk.Checkbutton(
            f,
            text="재부팅 시 자동 실행",
            variable=self._autostart_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))
        ttk.Label(
            f,
            text="Windows Startup 폴더에 바로가기를 추가합니다.",
            foreground="gray",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 8))

        ttk.Separator(f, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=4
        )

        # ── 자동 컨텍스트 주입 ──
        ttk.Label(f, text="세션 설정", font=("", 9, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 2)
        )
        ttk.Checkbutton(
            f,
            text="CLI 공급자 시작 시 자동으로 engram 컨텍스트 주입",
            variable=self._auto_inject_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        warn_frame = tk.Frame(f, bd=1, relief="solid", bg="#fff8e1")
        warn_frame.grid(row=6, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 8))
        tk.Label(
            warn_frame,
            text=(
                "⚠  활성화하면 세션 시작마다 engram_get_context 가 자동 호출됩니다.\n"
                "    초기 컨텍스트 토큰이 추가로 소모됩니다."
            ),
            bg="#fff8e1",
            anchor="w",
            justify="left",
            foreground="#7a5800",
        ).pack(fill="x", padx=8, pady=6)

        f.columnconfigure(1, weight=1)

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

        # 전역 탭
        self._autostart_var.set(_is_autostart_enabled())
        auto_inject = bool(self._engram_user_cfg.get("session", {}).get("auto_inject", False))
        self._auto_inject_var.set(auto_inject)

        self._load_persona_values()

    def _load_persona_values(self):
        user_persona = _safe_load_yaml(_USER_PERSONA_PATH)
        project_persona = _safe_load_yaml(_PROJECT_PERSONA_PATH)

        def _txt_set(widget: tk.Text, value: str) -> None:
            widget.delete("1.0", "end")
            if value:
                widget.insert("1.0", value)

        voice = user_persona.get("voice")
        _txt_set(self._persona_voice_txt, voice.strip() if isinstance(voice, str) else "")
        _txt_set(self._persona_traits_txt, ", ".join(_coerce_persona_list(user_persona.get("traits"))))
        _txt_set(self._persona_quirks_txt, ", ".join(_coerce_persona_list(user_persona.get("quirks"))))
        _txt_set(self._persona_values_txt, ", ".join(_coerce_persona_list(user_persona.get("values"))))
        fewshot = user_persona.get("fewshot")
        _txt_set(self._persona_fewshot_txt, fewshot.strip() if isinstance(fewshot, str) else "")

        for field in _PERSONA_NUMERIC_FIELDS:
            user_raw = user_persona.get(field)
            project_raw = project_persona.get(field)
            if isinstance(user_raw, (int, float)):
                value = _coerce_persona_number(user_raw, _PERSONA_DEFAULTS[field])
                pinned = True
            else:
                value = _coerce_persona_number(project_raw, _PERSONA_DEFAULTS[field])
                pinned = False

            self._persona_numeric_vars[field].set(value)
            self._persona_numeric_pin_vars[field].set(pinned)
            self._persona_numeric_label_vars[field].set(f"{value:.2f}")

        self._update_persona_banner(user_persona)

    def _update_persona_banner(self, user_persona: dict | None = None):
        if user_persona is None:
            user_persona = _safe_load_yaml(_USER_PERSONA_PATH)

        if _persona_has_custom_override(user_persona):
            self._persona_banner_var.set("현재 커스텀 페르소나가 적용되어 있습니다. 원하는 스타일로 계속 조정할 수 있습니다.")
        else:
            self._persona_banner_var.set("현재 기본 페르소나가 적용되어 있습니다. 커스텀 페르소나를 적용해 보세요.")

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

    def _ensure_user_persona_file(self) -> Path:
        if _USER_PERSONA_PATH.exists():
            return _USER_PERSONA_PATH
        _USER_PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_PERSONA_PATH.write_text(_PERSONA_USER_TEMPLATE, encoding="utf-8")
        return _USER_PERSONA_PATH

    def _render_persona_user_yaml(
        self,
        persona_values: dict,
        numeric_values: dict[str, float],
        pin_map: dict[str, bool],
    ) -> str:
        lines = [
            "# persona.user.yaml — 사용자 페르소나 오버라이드",
            "# 값이 있는 필드는 DB 진화값보다 우선 적용됩니다.",
            "# 슬라이더 pin이 해제된 항목은 주석(adaptive) 상태로 기록됩니다.",
            "",
            '# name: ""',
            "",
        ]

        body = yaml.safe_dump(persona_values, allow_unicode=True, sort_keys=False).strip()
        if body:
            lines.append(body)
            lines.append("")

        lines.append("# --- adaptive sliders (pin off) ---")
        adaptive_count = 0
        for field in _PERSONA_NUMERIC_FIELDS:
            if not pin_map[field]:
                lines.append(f"# [adaptive] {field}: {numeric_values[field]:.2f}")
                adaptive_count += 1
        if adaptive_count == 0:
            lines.append("# (none)")

        return "\n".join(lines).rstrip() + "\n"

    def _save_persona_user_file(self) -> int:
        self._ensure_user_persona_file()

        persona_values: dict = {}
        voice = self._persona_voice_txt.get("1.0", "end-1c").strip()
        if voice:
            persona_values["voice"] = voice

        traits = _parse_csv_field(self._persona_traits_txt.get("1.0", "end-1c"))
        if traits:
            persona_values["traits"] = traits

        quirks = _parse_csv_field(self._persona_quirks_txt.get("1.0", "end-1c"))
        if quirks:
            persona_values["quirks"] = quirks

        values = _parse_csv_field(self._persona_values_txt.get("1.0", "end-1c"))
        if values:
            persona_values["values"] = values

        fewshot = self._persona_fewshot_txt.get("1.0", "end-1c").strip()
        if fewshot:
            persona_values["fewshot"] = fewshot

        pin_map: dict[str, bool] = {}
        numeric_values: dict[str, float] = {}
        pinned_count = 0
        for field in _PERSONA_NUMERIC_FIELDS:
            numeric = _coerce_persona_number(self._persona_numeric_vars[field].get(), _PERSONA_DEFAULTS[field])
            pinned = bool(self._persona_numeric_pin_vars[field].get())
            pin_map[field] = pinned
            numeric_values[field] = numeric
            if pinned:
                persona_values[field] = numeric
                pinned_count += 1

        rendered = self._render_persona_user_yaml(persona_values, numeric_values, pin_map)
        _USER_PERSONA_PATH.write_text(rendered, encoding="utf-8")
        return pinned_count

    def _open_persona_file(self):
        try:
            path = self._ensure_user_persona_file()
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror("열기 실패", f"persona 파일을 열 수 없습니다.\n{e}", parent=self.window)

    def _show_toast(self, text: str):
        self._save_feedback_var.set(text)
        if self._toast_after_id:
            try:
                self.window.after_cancel(self._toast_after_id)
            except Exception:
                pass
        self._toast_after_id = self.window.after(2400, lambda: self._save_feedback_var.set(""))

    # ──────────────────────────────────────────────────────── 저장 ──

    def _save(self):
        try:
            pinned_count = self._do_save()
            self._update_persona_banner()
            if self._on_saved:
                try:
                    self._on_saved()
                except Exception:
                    pass
            self._show_toast(f"저장되었습니다. 슬라이더 고정 {pinned_count}/4, 나머지는 adaptive로 유지됩니다.")
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

        # 파일 쓰기 (overlay.user.yaml)
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(
            yaml.safe_dump(user, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # ── 전역 탭 — user.config.yaml ──
        engram_user = _safe_load_yaml(_ENGRAM_USER_CONFIG_PATH)
        auto_inject = bool(self._auto_inject_var.get())
        _nested_set(engram_user, ["session", "auto_inject"], auto_inject if auto_inject else None)
        _ENGRAM_USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ENGRAM_USER_CONFIG_PATH.write_text(
            yaml.safe_dump(engram_user, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # ── 전역 탭 — 자동 시작 토글 ──
        _set_autostart(bool(self._autostart_var.get()))

        return self._save_persona_user_file()

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
