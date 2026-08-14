"""overlay 설정 GUI — tkinter 기반 설정 다이얼로그.

오버레이 우클릭 컨텍스트 메뉴 또는 트레이 아이콘 → '설정'을 누르면 열림.
변경한 값만 ~/.engram/overlay.user.yaml 에 저장한다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox
from typing import Callable

import yaml

from overlay.bubble import shapes
from overlay.config import (
    _ENGRAM_USER_CONFIG_PATH,
    _USER_CONFIG_PATH,
    _safe_load_yaml,
    get_ollama_model,
    load_cfg,
    normalize_chat_mode,
    normalize_cli_provider,
    normalize_permission_level,
)
from core.identity import set_persona_baseline
from core.tutorial import complete_tutorial_step, has_user_persona_override, reset_tutorial_state

_PROVIDER_OPTIONS = [
    "copilot",
    "gemini",
    "claude-code",
    "claude-code(ollama)",
    "ollama",
]
_PROVIDER_DISPLAY_TO_VALUE = {
    "copilot": "copilot",
    "gemini": "gemini",
    "claude-code": "claude-code",
    "claude-code(ollama)": "claude-code-ollama",
    "ollama": "ollama",
}
_PROVIDER_VALUE_TO_DISPLAY = {
    "copilot": "copilot",
    "gemini": "gemini",
    "claude-code": "claude-code",
    "claude-code-ollama": "claude-code(ollama)",
    "ollama": "ollama",
}
_CHAT_MODE_OPTIONS = ["터미널 (TUI)", "말풍선 (실험적, 준비 중)"]
_CHAT_MODE_DISPLAY_TO_VALUE = {
    "터미널 (TUI)": "tui",
    "말풍선 (실험적, 준비 중)": "bubble",
}
_CHAT_MODE_VALUE_TO_DISPLAY = {
    "tui": "터미널 (TUI)",
    "bubble": "말풍선 (실험적, 준비 중)",
}

_PERMISSION_LEVEL_OPTIONS = ["자동 (승인 없음)", "위험한 작업만 확인", "항상 확인"]
_PERMISSION_LEVEL_DISPLAY_TO_VALUE = {
    "자동 (승인 없음)": "auto",
    "위험한 작업만 확인": "confirm_risky",
    "항상 확인": "confirm_always",
}
_PERMISSION_LEVEL_VALUE_TO_DISPLAY = {
    "auto": "자동 (승인 없음)",
    "confirm_risky": "위험한 작업만 확인",
    "confirm_always": "항상 확인",
}
_THOUGHT_DETAIL_OPTIONS = ["상세 — 추론 내용 표시 (CLI가 줄 때만)", "간략 — 상태 문구만"]
_THOUGHT_DETAIL_DISPLAY_TO_VALUE = {
    "상세 — 추론 내용 표시 (CLI가 줄 때만)": "full",
    "간략 — 상태 문구만": "brief",
}
_THOUGHT_DETAIL_VALUE_TO_DISPLAY = {
    "full": "상세 — 추론 내용 표시 (CLI가 줄 때만)",
    "brief": "간략 — 상태 문구만",
}

_THEME_COLOR_ROWS = [
    ("speech_bg", "대화풍선 배경"),
    ("speech_outline", "대화풍선 테두리"),
    ("speech_fg", "대화풍선 글자"),
    ("thought_bg", "생각풍선 배경"),
    ("thought_outline", "생각풍선 테두리"),
    ("thought_fg", "생각풍선 글자"),
    ("thought_tool_fg", "도구 상태 글자"),
    ("input_bg", "입력창 배경"),
    ("input_outline", "입력창 테두리"),
    ("tool_bg", "승인풍선 배경"),
    ("tool_outline", "승인풍선 테두리"),
    ("tool_fg", "승인풍선 글자"),
    ("nudge_bg", "능동발화 배경"),
    ("nudge_outline", "능동발화 테두리"),
    ("nudge_fg", "능동발화 글자"),
]

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
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
_STARTUP_LINK = _STARTUP_DIR / "engram-overlay.lnk"
_OVERLAY_CMD = Path.home() / ".engram" / "engram-overlay.cmd"
_OVERLAY_EXE: Path | None = None  # resolved lazily


def _resolve_overlay_target() -> Path | None:
    if _OVERLAY_CMD.exists():
        return _OVERLAY_CMD
    if getattr(sys, "frozen", False):
        current_exe = Path(sys.executable)
        if current_exe.exists():
            return current_exe
    global _OVERLAY_EXE
    if _OVERLAY_EXE and _OVERLAY_EXE.exists():
        return _OVERLAY_EXE
    return None


def _is_autostart_enabled() -> bool:
    return _STARTUP_LINK.exists()


def _set_autostart(enabled: bool) -> None:
    if enabled:
        # 설치 직후에는 installer가 이미 유효한 바로가기를 만든다. 설정을 저장할
        # 때마다 이를 재생성하면 PATH용 .cmd가 없는 배포 형태에서 불필요하게 실패한다.
        if _STARTUP_LINK.exists():
            return
        target = _resolve_overlay_target()
        if target is None:
            raise RuntimeError("자동 시작에 사용할 Engram Overlay 실행 파일을 찾을 수 없습니다.")
        _STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        ps = (
            f"$s = New-Object -ComObject WScript.Shell; "
            f"$sc = $s.CreateShortcut('{ _STARTUP_LINK }'); "
            f"$sc.TargetPath = '{ target }'; "
            f"$sc.WorkingDirectory = '{ target.parent }'; "
            f"$sc.Description = 'Engram Overlay \u2014 Auto Start'; "
            f"$sc.Save()"
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


def _is_port_listening(port: int) -> bool:
    # Tk 메인 스레드에서 2초마다 호출되므로 타임아웃을 짧게 둔다.
    # loopback 이라 닫힌 포트는 즉시 RST 가 오고, 이 값은 방화벽이 드롭할 때만 쓰인다.
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _format_tunnel_state(st) -> str:
    """TunnelStatus 를 사람이 읽는 한 줄로."""
    if st is None:
        return "○ 미시작"
    from overlay.remote_tunnel import (
        STATE_AUTH_FAILED,
        STATE_CONNECTING,
        STATE_DOWN,
        STATE_FAILED,
        STATE_UP,
    )

    if st.state == STATE_UP:
        mins = int(st.uptime_secs() // 60)
        tail = f", 재연결 {st.retries}" if st.retries else ""
        return f"● 연결됨 ({mins}분{tail})"
    if st.state == STATE_CONNECTING:
        from overlay.remote_tunnel import sanitize_for_display

        # 콘솔 로그인 대기처럼 "왜 안 끝나는지" 가 있으면 그대로 보여준다.
        detail = sanitize_for_display(st.last_error, 60)
        return f"◐ {detail}" if detail else "◐ 연결 중…"
    if st.state == STATE_AUTH_FAILED:
        return "✖ 키 인증 필요 — [키 등록]"
    if st.state == STATE_FAILED:
        from overlay.remote_tunnel import sanitize_for_display

        return f"✖ 실패({st.retries}) {sanitize_for_display(st.last_error, 60)}"
    if st.state == STATE_DOWN:
        return "○ 끊김"
    return st.state


def _audit_tail(n: int) -> str:
    """원격 감사 로그 꼬리. 토큰 값은 애초에 기록되지 않는다.

    tool/path 는 원격 호출자가 정하는 값이다. 개행이 섞이면 가짜 행을 만들어
    로그를 위조해 보이게 할 수 있으므로 표시 전에 살균한다.
    """
    import json

    from overlay.remote_tunnel import sanitize_for_display

    path = Path.home() / ".engram" / "logs" / "remote-audit.jsonl"
    if not path.exists():
        return "(기록 없음)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception as e:
        return f"(읽기 실패: {e})"
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = sanitize_for_display(str(d.get("ts", ""))[11:19], 8)
        action = sanitize_for_display(str(d.get("action", "?")), 12)
        what = sanitize_for_display(str(d.get("tool") or d.get("path", "")), 40)
        who = sanitize_for_display(str(d.get("principal", "-")), 24)
        out.append(f"{ts} {action:<12} {what} ({who})")
    return "\n".join(out) if out else "(기록 없음)"


def open_settings(
    root: tk.Tk,
    on_saved: Callable[[], None] | None = None,
    on_get_ollama_models: Callable[[], list[str]] | None = None,
    on_reload_ollama_models: Callable[[], None] | None = None,
    tunnels=None,
) -> None:
    """설정 창을 열거나 이미 열려 있으면 포커스를 줍니다."""
    for widget in root.winfo_children():
        if isinstance(widget, tk.Toplevel) and getattr(widget, "_is_settings_window", False):
            widget.lift()
            widget.focus_force()
            return

    win = _SettingsWindow(
        root,
        on_saved=on_saved,
        on_get_ollama_models=on_get_ollama_models,
        on_reload_ollama_models=on_reload_ollama_models,
        tunnels=tunnels,
    )
    win.window.focus_force()


class _SettingsWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_saved: Callable[[], None] | None = None,
        on_get_ollama_models: Callable[[], list[str]] | None = None,
        on_reload_ollama_models: Callable[[], None] | None = None,
        tunnels=None,
    ):
        self._root = root
        self._on_saved = on_saved
        self._on_get_ollama_models = on_get_ollama_models
        self._on_reload_ollama_models = on_reload_ollama_models
        self._tunnels = tunnels  # overlay.remote_tunnel.TunnelManager | None
        self._remote_after_id: str | None = None
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
        self._dashboard_enabled_var = tk.BooleanVar()
        # 능동 발화(initiative) — 빈도/타이밍 knob 들을 GUI 로 노출.
        self._initiative_enabled_var = tk.BooleanVar()
        self._initiative_idle_min_var = tk.IntVar()      # 유휴 대기(분)
        self._initiative_gap_min_var = tk.IntVar()       # 최소 간격(분)
        self._initiative_quiet_on_var = tk.BooleanVar()  # 조용한 시간대 사용
        self._initiative_quiet_start_var = tk.IntVar()   # 시작 시(0-23)
        self._initiative_quiet_end_var = tk.IntVar()     # 끝 시(0-23)
        self._initiative_phrasing_var = tk.BooleanVar()  # LLM 문구 다듬기(하이브리드)

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
        self._tab_bubble = ttk.Frame(notebook)
        self._tab_remote = ttk.Frame(notebook)
        self._tab_global = ttk.Frame(notebook)

        notebook.add(self._tab_overlay, text="오버레이")
        notebook.add(self._tab_cli, text="CLI 공급자")
        notebook.add(self._tab_persona, text="페르소나")
        notebook.add(self._tab_terminal, text="터미널")
        notebook.add(self._tab_bubble, text="말풍선")
        notebook.add(self._tab_remote, text="원격")
        notebook.add(self._tab_global, text="전역")

        self._build_overlay_tab(PAD)
        self._build_cli_tab(PAD)
        self._build_persona_tab(PAD)
        self._build_terminal_tab(PAD)
        self._build_bubble_theme_tab(PAD)
        self._build_remote_tab(PAD)
        self._build_global_tab(PAD)

        self._save_feedback_var = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self._save_feedback_var, foreground="gray").pack(fill="x", padx=12, pady=(0, 4))

        # 하단 버튼
        btn_frame = ttk.Frame(self.window)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="저장", command=self._save).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frame, text="취소", command=self._close).pack(side="right")
        # 원격 탭이 after() 로 상태를 폴링하므로 닫을 때 반드시 취소한다.
        self.window.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        if self._remote_after_id:
            try:
                self.window.after_cancel(self._remote_after_id)
            except Exception:
                pass
            self._remote_after_id = None
        try:
            self.window.destroy()
        except Exception:
            pass

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
        self._flip_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="좌우 반전", variable=self._flip_var).grid(row=0, column=3, sticky="w", padx=(4, 8), pady=4)
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

        # 채팅 UI 모드
        ttk.Label(f, text="채팅 UI 모드:").grid(row=4, column=0, sticky="w", **PAD)
        self._chat_mode_var = tk.StringVar()
        ttk.Combobox(
            f,
            textvariable=self._chat_mode_var,
            values=_CHAT_MODE_OPTIONS,
            state="readonly",
            width=22,
        ).grid(row=4, column=1, sticky="ew", **PAD)
        ttk.Label(
            f,
            text="말풍선 모드는 아직 미구현이라 선택해도 터미널로 동작합니다.",
            foreground="gray",
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        # 말풍선 모드 권한 수준
        ttk.Label(f, text="말풍선 권한 수준:").grid(row=6, column=0, sticky="w", **PAD)
        self._permission_level_var = tk.StringVar()
        ttk.Combobox(
            f,
            textvariable=self._permission_level_var,
            values=_PERMISSION_LEVEL_OPTIONS,
            state="readonly",
            width=22,
        ).grid(row=6, column=1, sticky="ew", **PAD)
        ttk.Label(
            f,
            text="말풍선 모드에서 도구 사용을 얼마나 자동으로 승인할지 (기본: 자동).",
            foreground="gray",
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        # ── 능동 발화 (initiative) — 유휴 시 캐릭터가 스스로 말을 건다(말풍선 모드 전용) ──
        init_box = ttk.LabelFrame(f, text="능동 발화 — 유휴 시 스스로 말 걸기")
        init_box.grid(row=8, column=0, columnspan=3, sticky="we", padx=8, pady=(10, 4))

        ttk.Checkbutton(
            init_box, text="능동 발화 사용", variable=self._initiative_enabled_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        ttk.Label(init_box, text="유휴 대기:").grid(row=1, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(init_box, textvariable=self._initiative_idle_min_var, from_=1, to=120, width=6).grid(
            row=1, column=1, sticky="w", pady=3)
        ttk.Label(init_box, text="분 이상 조용하면").grid(row=1, column=2, columnspan=2, sticky="w", padx=(2, 8), pady=3)

        ttk.Label(init_box, text="최소 간격:").grid(row=2, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(init_box, textvariable=self._initiative_gap_min_var, from_=1, to=240, width=6).grid(
            row=2, column=1, sticky="w", pady=3)
        ttk.Label(init_box, text="분마다 최대 1번").grid(row=2, column=2, columnspan=2, sticky="w", padx=(2, 8), pady=3)

        ttk.Checkbutton(
            init_box, text="조용한 시간대", variable=self._initiative_quiet_on_var,
        ).grid(row=3, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(init_box, textvariable=self._initiative_quiet_start_var, from_=0, to=23, width=4).grid(
            row=3, column=1, sticky="w", pady=3)
        ttk.Label(init_box, text="시 ~").grid(row=3, column=2, sticky="w", pady=3)
        ttk.Spinbox(init_box, textvariable=self._initiative_quiet_end_var, from_=0, to=23, width=4).grid(
            row=3, column=3, sticky="w", pady=3)

        ttk.Checkbutton(
            init_box, text="LLM 문구 다듬기(하이브리드) — 끄면 토큰 0·문구 단조",
            variable=self._initiative_phrasing_var,
        ).grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 2))

        ttk.Label(
            init_box,
            text="미완 작업·호기심·git 상태 등을 가끔 먼저 건넵니다. 세부(소스별 on/off·쿨다운)는 overlay.yaml 의 bubble.initiative 로 조절.",
            foreground="gray", wraplength=440, justify="left",
        ).grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 6))
        init_box.columnconfigure(3, weight=1)

        f.columnconfigure(1, weight=1)

    def _build_cli_tab(self, PAD: dict):
        f = self._tab_cli

        # 공급자 선택
        ttk.Label(f, text="기본 공급자:").grid(row=0, column=0, sticky="w", **PAD)
        self._provider_var = tk.StringVar()
        provider_combo = ttk.Combobox(f, textvariable=self._provider_var, values=_PROVIDER_OPTIONS, state="readonly", width=18)
        provider_combo.grid(row=0, column=1, sticky="ew", **PAD)

        # Ollama 모델
        ttk.Label(f, text="Ollama 모델:").grid(row=1, column=0, sticky="w", **PAD)
        self._ollama_model_var = tk.StringVar()
        model_frame = ttk.Frame(f)
        model_frame.grid(row=1, column=1, sticky="ew", **PAD)
        model_frame.columnconfigure(0, weight=1)
        self._ollama_model_combo = ttk.Combobox(model_frame, textvariable=self._ollama_model_var, width=19)
        self._ollama_model_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(model_frame, text="↻", width=3, command=self._refresh_ollama_models).grid(row=0, column=1, padx=(4, 0))

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
        ttk.Button(f, text="페르소나 파일 열기", command=self._open_persona_file).grid(
            row=6, column=0, columnspan=2, sticky="e", padx=8, pady=(0, 6)
        )

        f.columnconfigure(1, weight=1)

    def _refresh_ollama_models(self) -> None:
        """Ollama 백엔드에서 모델 목록을 새로고침하고 Combobox를 업데이트한다."""
        if self._on_reload_ollama_models:
            self._on_reload_ollama_models()
        # 새로고침 후 잠시 뒤 목록 반영 (백그라운드 로드 완료 대기)
        self.window.after(1500, self._update_ollama_model_combo)

    def _update_ollama_model_combo(self) -> None:
        models = self._on_get_ollama_models() if self._on_get_ollama_models else []
        current = self._ollama_model_var.get()
        self._ollama_model_combo["values"] = models
        if models and current not in models:
            self._ollama_model_combo.set(models[0])

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
            grip,
            text="·  ·  ·",
            bg="#e0e0e0",
            foreground="#a8a8a8",
            font=("TkDefaultFont", 7),
            anchor="e",
        ).place(relx=1.0, rely=0.5, anchor="e", x=-4)

        grip._drag_y = 0  # type: ignore[attr-defined]
        grip._txt = txt  # type: ignore[attr-defined]

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

    def _build_bubble_theme_tab(self, PAD: dict):
        import tkinter.font as tkfont

        f = self._tab_bubble
        self._theme_vars: dict[str, tk.StringVar] = {}
        self._theme_swatches: dict[str, tk.Button] = {}

        ttk.Label(
            f,
            text="말풍선 모드(실험적)의 글꼴과 색상 테마 — 저장하면 실행 중인 오버레이에 바로 반영됩니다.",
            foreground="gray",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 8))

        # 글꼴 — @로 시작하는 세로쓰기 변형 제외, 중복 제거 후 정렬.
        families = sorted({fam for fam in tkfont.families() if not fam.startswith("@")})
        ttk.Label(f, text="글꼴:").grid(row=1, column=0, sticky="w", **PAD)
        self._bubble_font_var = tk.StringVar()
        font_combo = ttk.Combobox(f, textvariable=self._bubble_font_var, values=families, width=28, state="readonly")
        font_combo.grid(row=1, column=1, columnspan=2, sticky="w", **PAD)
        font_combo.bind("<<ComboboxSelected>>", self._update_font_preview)

        # 폰트 크기 — 0이면 자동(TUI 스케일). 양수면 고정 px.
        ttk.Label(f, text="폰트 크기:").grid(row=2, column=0, sticky="w", **PAD)
        size_frame = ttk.Frame(f)
        size_frame.grid(row=2, column=1, columnspan=2, sticky="w", **PAD)
        self._bubble_font_size_var = tk.IntVar()
        ttk.Spinbox(
            size_frame, textvariable=self._bubble_font_size_var, from_=0, to=48, width=6,
            command=self._update_font_preview,
        ).pack(side="left")
        ttk.Label(size_frame, text="(0 = 자동)", foreground="gray").pack(side="left", padx=(6, 0))
        # 스핀박스에 직접 타이핑할 때도 갱신되게 변수 트레이스.
        self._bubble_font_size_var.trace_add("write", lambda *_: self._update_font_preview())

        # 미리보기 — IDE처럼 실제 글꼴/크기/색으로 샘플 텍스트를 보여준다(저장 전에 확인).
        ttk.Label(f, text="미리보기:").grid(row=3, column=0, sticky="nw", **PAD)
        self._font_preview = tk.Label(
            f, justify="left", anchor="w", relief="solid", bd=1, padx=12, pady=10,
            text="말풍선 미리보기\n안녕하세요 Hello 0123 가나다라마",
        )
        self._font_preview.grid(row=3, column=1, columnspan=2, sticky="we", **PAD)

        for i, (key, label) in enumerate(_THEME_COLOR_ROWS, start=4):
            self._add_theme_color_row(f, i, label, key, PAD)

        # ── 자동 페이드아웃 — 입력/응답/생각 풍선 각각 on/off + 유지 시간(초) ──
        fade_row = 4 + len(_THEME_COLOR_ROWS)
        fade_box = ttk.LabelFrame(f, text="자동 사라짐 (fade-out)")
        fade_box.grid(row=fade_row, column=0, columnspan=3, sticky="we", padx=8, pady=(10, 4))
        self._fade_vars: dict[str, tk.BooleanVar] = {}
        self._fade_secs: dict[str, tk.DoubleVar] = {}
        for r, (key, label, hint) in enumerate((
            ("echo", "입력(내 메시지)", "보낸 뒤"),
            ("speech", "응답", "생성 끝난 뒤"),
            ("thought", "생각", "응답 끝나면"),
        )):
            on_var = tk.BooleanVar()
            self._fade_vars[key] = on_var
            ttk.Checkbutton(fade_box, text=label, variable=on_var, width=16).grid(row=r, column=0, sticky="w", padx=8, pady=3)
            sec_var = tk.DoubleVar()
            self._fade_secs[key] = sec_var
            ttk.Label(fade_box, text=hint).grid(row=r, column=1, sticky="e", padx=(8, 2), pady=3)
            ttk.Spinbox(fade_box, textvariable=sec_var, from_=0.0, to=120.0, increment=0.5, width=6).grid(
                row=r, column=2, sticky="w", pady=3
            )
            ttk.Label(fade_box, text="초 뒤").grid(row=r, column=3, sticky="w", padx=(2, 8), pady=3)
        fade_box.columnconfigure(1, weight=1)

        # ── 최대 높이 비율 (0 = 무제한) ──
        height_row = fade_row + 1
        h_box = ttk.LabelFrame(f, text="최대 높이 (화면 작업영역 비율, 0 = 무제한)")
        h_box.grid(row=height_row, column=0, columnspan=3, sticky="we", padx=8, pady=(6, 4))
        self._speech_max_h_var = tk.DoubleVar()
        self._thought_max_h_var = tk.DoubleVar()
        for r, (var, label, default_val) in enumerate((
            (self._speech_max_h_var, "응답 풍선", 0.55),
            (self._thought_max_h_var, "생각 풍선", 0.30),
        )):
            lbl_var = tk.StringVar()

            def _make_update(v=var, lv=lbl_var):
                def _upd(*_):
                    val = float(v.get())
                    lv.set("무제한" if val == 0 else f"{val:.2f}")
                return _upd

            updater = _make_update()
            var.trace_add("write", updater)
            ttk.Label(h_box, text=label, width=12).grid(row=r, column=0, sticky="w", padx=8, pady=3)
            ttk.Scale(
                h_box, from_=0.0, to=1.0, variable=var,
                orient="horizontal", length=160,
                command=lambda v, lv=lbl_var: lv.set("무제한" if float(v) == 0 else f"{float(v):.2f}"),
            ).grid(row=r, column=1, sticky="w", pady=3)
            ttk.Label(h_box, textvariable=lbl_var, width=8).grid(row=r, column=2, sticky="w", padx=(4, 8), pady=3)
            lbl_var.set("무제한" if default_val == 0 else f"{default_val:.2f}")

        # ── 생각 풍선 표시 방식 ──
        detail_row = height_row + 1
        d_box = ttk.LabelFrame(f, text="생각 풍선 내용")
        d_box.grid(row=detail_row, column=0, columnspan=3, sticky="we", padx=8, pady=(6, 4))
        self._thought_detail_var = tk.StringVar()
        ttk.Combobox(
            d_box, textvariable=self._thought_detail_var,
            values=_THOUGHT_DETAIL_OPTIONS, state="readonly", width=30,
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(
            d_box,
            text="최신 Claude Code CLI는 추론 텍스트를 감추고 진행 상황만 보냅니다 — 이 경우\n"
                 "'상세'로 둬도 \"생각을 정리하는 중…\" 같은 문구만 나옵니다(오버레이가 만들어낼 수\n"
                 "없는 내용입니다). '간략'은 CLI가 내용을 주더라도 항상 짧은 문구로 고정합니다.",
            foreground="gray", justify="left",
        ).grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))

        f.columnconfigure(1, weight=1)

    def _preview_font_size(self) -> int:
        try:
            size = int(self._bubble_font_size_var.get())
        except (tk.TclError, ValueError):
            return 11
        if size > 0:
            return size
        # 자동 — 설정 창이 떠 있는 모니터 기준으로 TUI 스케일 폰트 크기를 근사해서 보여준다.
        try:
            from overlay.chat_window import terminal_font_size

            tcfg = {
                "base_font_size": _nested_get(self._cfg, ["terminal", "base_font_size"], 8),
                "ref_screen_height": _nested_get(self._cfg, ["terminal", "ref_screen_height"], 1080),
            }
            x, y = self.window.winfo_x(), self.window.winfo_y()
            return max(8, round(terminal_font_size(x, y, tcfg)))
        except Exception:
            return 12

    def _update_font_preview(self, *_args) -> None:
        if not hasattr(self, "_font_preview"):
            return
        fam = self._bubble_font_var.get() or "Noto Sans KR Medium"
        size = self._preview_font_size()
        bg = (self._theme_vars.get("speech_bg").get() if self._theme_vars.get("speech_bg") else "") or shapes.DEFAULT_THEME["speech_bg"]
        fg = (self._theme_vars.get("speech_fg").get() if self._theme_vars.get("speech_fg") else "") or shapes.DEFAULT_THEME["speech_fg"]
        try:
            self._font_preview.config(font=(fam, size), bg=bg, fg=fg)
        except tk.TclError:
            pass

    def _add_theme_color_row(self, parent, row: int, label: str, key: str, PAD: dict):
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=0, sticky="w", **PAD)
        var = tk.StringVar()
        self._theme_vars[key] = var
        swatch = tk.Button(parent, width=10, command=lambda k=key: self._pick_theme_color(k))
        swatch.grid(row=row, column=1, sticky="w", **PAD)
        self._theme_swatches[key] = swatch
        ttk.Button(
            parent, text="기본값", width=6, command=lambda k=key: self._reset_theme_color(k)
        ).grid(row=row, column=2, sticky="w", **PAD)

    def _pick_theme_color(self, key: str) -> None:
        current = self._theme_vars[key].get() or shapes.DEFAULT_THEME[key]
        try:
            _rgb, hex_color = colorchooser.askcolor(color=current, parent=self.window, title="색상 선택")
        except tk.TclError:
            _rgb, hex_color = colorchooser.askcolor(parent=self.window, title="색상 선택")
        if hex_color:
            self._theme_vars[key].set(hex_color)
            self._update_theme_swatch(key)

    def _reset_theme_color(self, key: str) -> None:
        self._theme_vars[key].set(shapes.DEFAULT_THEME[key])
        self._update_theme_swatch(key)

    def _update_theme_swatch(self, key: str) -> None:
        color = self._theme_vars[key].get() or shapes.DEFAULT_THEME[key]
        btn = self._theme_swatches[key]
        try:
            btn.config(bg=color, activebackground=color, text=color)
        except tk.TclError:
            pass
        # speech 색을 바꾸면 미리보기도 그 색으로 갱신(글꼴 미리보기가 실제 말풍선 색을 반영).
        if key in ("speech_bg", "speech_fg"):
            self._update_font_preview()

    # ── 원격 탭 ──────────────────────────────────────────────────────────────
    def _build_remote_tab(self, PAD: dict):
        f = self._tab_remote

        # 리스너
        lf = ttk.LabelFrame(f, text="인증 리스너")
        lf.pack(fill="x", padx=8, pady=(8, 4))
        self._remote_enabled_var = tk.BooleanVar()
        ttk.Checkbutton(
            lf, text="원격 인증 리스너 사용", variable=self._remote_enabled_var
        ).grid(row=0, column=0, sticky="w", **PAD)
        ttk.Label(lf, text="포트:").grid(row=0, column=1, sticky="e", **PAD)
        self._remote_port_var = tk.IntVar(value=17386)
        ttk.Spinbox(lf, textvariable=self._remote_port_var, from_=1024, to=65535, width=8).grid(
            row=0, column=2, sticky="w", **PAD
        )
        self._remote_listener_var = tk.StringVar(value="확인 중…")
        ttk.Label(lf, textvariable=self._remote_listener_var).grid(row=0, column=3, sticky="w", **PAD)
        ttk.Label(
            lf,
            text="터널은 이 포트에만 연결한다. 로컬 포트는 무인증이므로 노출하면 안 된다.",
            foreground="gray",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        # 토큰 (값은 표시하지 않는다)
        tf = ttk.LabelFrame(f, text="토큰")
        tf.pack(fill="x", padx=8, pady=4)
        self._token_list_var = tk.StringVar(value="")
        ttk.Label(tf, textvariable=self._token_list_var, justify="left").pack(
            side="left", padx=8, pady=6
        )
        ttk.Button(tf, text="토큰 파일 열기", command=self._open_tokens_file).pack(
            side="right", padx=6, pady=4
        )

        # 터널
        nf = ttk.LabelFrame(f, text="터널")
        nf.pack(fill="x", padx=8, pady=4)
        ttk.Label(
            nf,
            text="목록은 오버레이를 재시작해도 유지된다. 연결은 [연결]을 눌러 그때 로그인한다.",
            foreground="gray",
        ).pack(anchor="w", padx=8, pady=(4, 0))

        self._tunnel_tree = ttk.Treeview(
            nf, columns=("state",), show="tree headings", height=6
        )
        self._tunnel_tree.heading("#0", text="호스트")
        self._tunnel_tree.heading("state", text="상태")
        self._tunnel_tree.column("#0", width=200)
        self._tunnel_tree.column("state", width=340)
        # expand 하지 않는다 — 늘어나면 빈 공간만 커지고 아래 패널이 밀린다.
        self._tunnel_tree.pack(fill="x", padx=6, pady=(6, 2))

        bar = ttk.Frame(nf)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        self._tunnel_add_var = tk.StringVar()
        self._tunnel_add_combo = ttk.Combobox(
            bar, textvariable=self._tunnel_add_var, width=22, state="readonly"
        )
        self._tunnel_add_combo.pack(side="left")
        ttk.Button(bar, text="추가", command=self._add_tunnel).pack(side="left", padx=(4, 8))
        ttk.Button(bar, text="제거", command=self._remove_tunnel).pack(side="left")
        ttk.Button(bar, text="연결", command=lambda: self._tunnel_action("start")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(bar, text="끊기", command=lambda: self._tunnel_action("stop")).pack(
            side="left", padx=4
        )
        ttk.Button(bar, text="창 숨기기", command=self._hide_tunnel_console).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(bar, text="키 등록", command=self._install_ssh_key).pack(side="left", padx=(8, 0))
        self._tunnel_autoreconnect_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar,
            text="끊기면 자동 재연결",
            variable=self._tunnel_autoreconnect_var,
        ).pack(side="right")

        # 최근 원격 접근
        af = ttk.LabelFrame(f, text="최근 원격 접근")
        af.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self._audit_var = tk.StringVar(value="(없음)")
        ttk.Label(af, textvariable=self._audit_var, justify="left", foreground="#444").pack(
            anchor="nw", padx=8, pady=6
        )

        self._tunnel_rows: list[str] = []
        self._load_remote_tab()
        self._refresh_remote_status()

    # ── 원격 탭 데이터 ───────────────────────────────────────────────────────
    def _load_remote_tab(self):
        from overlay.remote_tunnel import ssh_host_aliases

        mcp = _nested_get(self._cfg, ["mcp"], {}) or {}
        self._remote_enabled_var.set(bool(mcp.get("remote_enabled", False)))
        self._remote_port_var.set(int(mcp.get("remote_port", 17386) or 17386))

        self._tunnel_add_combo["values"] = ssh_host_aliases()
        self._tunnel_autoreconnect_var.set(bool(mcp.get("tunnel_auto_reconnect", False)))

        self._tunnel_rows = []
        for entry in mcp.get("tunnels") or []:
            if isinstance(entry, dict) and entry.get("host"):
                host = str(entry["host"])
                if host not in self._tunnel_rows:
                    self._tunnel_rows.append(host)
        self._redraw_tunnel_tree()
        self._token_list_var.set(self._token_summary())

    def _token_summary(self) -> str:
        """토큰 name/scope 만 보여준다. 값은 절대 UI 에 싣지 않는다."""
        path = Path.home() / ".engram" / "mcp-tokens.yaml"
        if not path.exists():
            return "(토큰 파일 없음 — 리스너를 한 번 켜면 생성된다)"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            return f"(읽기 실패: {e})"
        rows = []
        for t in data.get("tokens") or []:
            if not isinstance(t, dict):
                continue
            rows.append(
                f"{str(t.get('name') or '?'):<18} scope={t.get('scope') or '(미지정 — global 폴백)'}"
            )
        return "\n".join(rows) if rows else "(등록된 토큰 없음 — 원격 요청은 모두 401)"

    def _open_tokens_file(self):
        path = Path.home() / ".engram" / "mcp-tokens.yaml"
        if not path.exists():
            messagebox.showinfo(
                "토큰 파일 없음",
                "원격 리스너를 한 번 켜면 자동 생성됩니다.",
                parent=self.window,
            )
            return
        try:
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror("열기 실패", str(e), parent=self.window)

    def _redraw_tunnel_tree(self):
        sel = self._selected_tunnel_host()
        for item in self._tunnel_tree.get_children():
            self._tunnel_tree.delete(item)
        for host in self._tunnel_rows:
            self._tunnel_tree.insert("", "end", iid=host, text=host, values=("",))
        if sel and sel in self._tunnel_rows:
            self._tunnel_tree.selection_set(sel)

    def _selected_tunnel_host(self) -> str:
        sel = self._tunnel_tree.selection()
        return sel[0] if sel else ""

    def _add_tunnel(self):
        host = self._tunnel_add_var.get().strip()
        if not host:
            messagebox.showinfo("대상 선택", "~/.ssh/config 의 Host 를 고르세요.", parent=self.window)
            return
        if host in self._tunnel_rows:
            return
        self._tunnel_rows.append(host)
        self._redraw_tunnel_tree()
        self._show_toast(f"{host} 추가됨 — 저장해야 재시작 후에도 남습니다.")

    def _remove_tunnel(self):
        host = self._selected_tunnel_host()
        if not host:
            return
        if host in self._tunnel_rows:
            self._tunnel_rows.remove(host)
        if self._tunnels:
            try:
                self._tunnels.remove(host)
            except Exception:
                pass
        self._redraw_tunnel_tree()

    def _tunnel_action(self, action: str):
        host = self._selected_tunnel_host()
        if not host:
            messagebox.showinfo("대상 선택", "목록에서 호스트를 고르세요.", parent=self.window)
            return
        if not self._tunnels:
            messagebox.showwarning(
                "사용 불가", "터널 관리자를 쓸 수 없습니다(오버레이 외부 실행).", parent=self.window
            )
            return
        try:
            if action == "start":
                self._tunnels.start(host)
            else:
                self._tunnels.stop(host)
        except Exception as e:
            messagebox.showerror("실패", str(e), parent=self.window)

    def _hide_tunnel_console(self):
        """콘솔 로그인을 마쳤다고 알려 창을 숨긴다.

        인증이 끝난 시점을 프로그램이 알 수 없어(-f 는 비밀번호 인증에서 동작하지
        않는다) 사용자가 알려주는 방식으로 둔다. 타이머로 숨기면 타이핑 도중에
        창이 사라진다.
        """
        host = self._selected_tunnel_host()
        if not host:
            messagebox.showinfo("대상 선택", "목록에서 호스트를 고르세요.", parent=self.window)
            return
        if not self._tunnels:
            return
        if self._tunnels.hide_console(host):
            self._show_toast(f"{host}: 콘솔 창을 숨겼습니다. 터널은 계속 유지됩니다.")
        else:
            messagebox.showinfo(
                "숨길 창 없음",
                f"'{host}' 에 열려 있는 콘솔 로그인 창이 없습니다.\n"
                "키 인증으로 붙은 터널은 애초에 창이 없습니다.",
                parent=self.window,
            )

    def _install_ssh_key(self):
        """공개키를 원격 authorized_keys 에 심는다.

        비밀번호가 필요하므로 CREATE_NEW_CONSOLE 로 실제 콘솔을 띄운다 —
        ssh 는 비밀번호를 stdin 이 아니라 TTY 에서 읽기 때문에 파이프로는 안 된다.
        """
        from overlay.remote_tunnel import is_safe_host, ssh_host_aliases

        host = self._selected_tunnel_host()
        if not host:
            messagebox.showinfo("대상 선택", "목록에서 호스트를 고르세요.", parent=self.window)
            return

        # 아래에서 cmd 문자열을 만든다 → 호스트를 신뢰하면 명령 주입이 된다.
        # (호스트는 overlay.user.yaml 에서도 올 수 있다.)
        # 화이트리스트(ssh_config 별칭) + 문자 검증을 모두 통과해야 한다.
        if not is_safe_host(host) or host not in ssh_host_aliases():
            messagebox.showerror(
                "허용되지 않는 호스트",
                f"'{host}' 는 ~/.ssh/config 의 Host 별칭이 아니거나 안전하지 않은 문자를 포함합니다.\n"
                "ssh_config 에 Host 항목으로 먼저 등록하세요.",
                parent=self.window,
            )
            return

        pub = Path.home() / ".ssh" / "id_ed25519.pub"
        if not pub.exists():
            if not messagebox.askyesno(
                "키 없음", "SSH 키가 없습니다. 지금 만들까요? (ed25519, 암호 없음)", parent=self.window
            ):
                return
            try:
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(pub.with_suffix(""))],
                    check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                messagebox.showerror("키 생성 실패", str(e), parent=self.window)
                return

        # POSIX 원격 기준. Windows 원격은 authorized_keys 위치와 ACL 요구가 달라 별도 처리가 필요하다.
        #
        # 반드시 커맨드라인을 "문자열"로 넘긴다. 리스트로 넘기면 Windows 에서
        # subprocess.list2cmdline 이 인자를 다시 인용하며 안쪽 " 를 \" 로 이스케이프해,
        # cmd 가 따옴표를 리터럴로 넘겨버린다(ssh 가 "host" 를 호스트명으로 인식).
        # 호스트는 바로 위에서 화이트리스트 + 문자셋 검증을 통과한 값만 여기 온다.
        # 원격 셸에 따라 명령이 완전히 다르다. POSIX 명령을 cmd.exe 에 보내면
        # 'umask' 를 못 찾고 아무것도 안 쓰인 채 끝나 "인증 실패"로만 보인다.
        # 한 줄로 양쪽을 처리하는 셸 폴리글롯은 검증이 어려워 쓰지 않고, 직접 묻는다.
        is_posix = messagebox.askyesno(
            "원격 OS",
            f"'{host}' 의 원격 OS 가 Linux/macOS 입니까?\n\n"
            "예 → Linux/macOS\n아니오 → Windows",
            parent=self.window,
        )
        if is_posix:
            inner = (
                "umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
                "cat >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
            )
        else:
            # Windows 원격은 자동 설치하지 않는다.
            #
            # 원격 SSH 의 기본 셸이 cmd 인지 PowerShell 인지에 따라 같은 문자열이
            # 전혀 다르게 동작한다. PowerShell 이면 '&' 는 호출 연산자, '2>nul' 은
            # nul 이라는 파일로의 리다이렉트, '>>' 는 UTF-16LE 로 쓰기다.
            # 그 결과 authorized_keys 에 UTF-16 쓰레기가 덧붙어 기존에 동작하던 키까지
            # 깨질 수 있다(실제로 그렇게 만들어 원격 접속을 끊어먹었다).
            # 여기에 더해 관리자 계정이면 Windows OpenSSH 는 이 파일을 아예 보지 않고
            # C:\ProgramData\ssh\administrators_authorized_keys 만 참조한다.
            #
            # 검증할 수 없는 셸 추측을 원격에 실행하는 대신 정확한 절차를 안내한다.
            key_line = ""
            try:
                key_line = pub.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            self._root.clipboard_clear()
            self._root.clipboard_append(key_line)
            messagebox.showinfo(
                "Windows 원격 — 수동 등록",
                "공개키를 클립보드에 복사했습니다.\n"
                f"({pub})\n\n"
                "원격 셸이 cmd 인지 PowerShell 인지에 따라 같은 명령이 다르게 동작해\n"
                "(PowerShell 은 '>>' 를 UTF-16 으로 씁니다) 자동 설치는 하지 않습니다.\n\n"
                "■ 먼저 관리자 계정인지 확인 — 원격에서\n"
                "    whoami /groups | findstr S-1-5-32-544\n"
                "  이 줄이 나오면 관리자입니다.\n\n"
                "■ 관리자인 경우 (중요)\n"
                "  Windows OpenSSH 는 관리자 계정의 ~/.ssh/authorized_keys 를\n"
                "  아예 읽지 않습니다. 아래 파일에 넣어야 합니다.\n\n"
                "    md C:\\ProgramData\\ssh 2>nul\n"
                "    echo <붙여넣기>>> C:\\ProgramData\\ssh\\administrators_authorized_keys\n"
                "    icacls C:\\ProgramData\\ssh\\administrators_authorized_keys "
                "/inheritance:r /grant *S-1-5-18:F /grant *S-1-5-32-544:F\n\n"
                "  ACL 을 좁히지 않으면 sshd 가 파일을 무시합니다.\n"
                "  SID 로 지정한 것은 한글 Windows 에서 그룹명이 다르기 때문입니다.\n\n"
                "■ 일반 계정인 경우\n"
                "    md .ssh 2>nul\n"
                "    echo <붙여넣기>>> .ssh\\authorized_keys\n\n"
                "인코딩은 UTF-8(BOM 없음) 또는 ASCII 여야 합니다.",
                parent=self.window,
            )
            return

        # NumberOfPasswordPrompts=1: 오타로 3회까지 재시도하며 인증 미완료 연결을 쌓지 않는다.
        # 반복 시도가 누적되면 원격 sshd 의 MaxStartups 가 포화돼 TCP 는 붙는데 배너가
        # 오지 않는 상태가 되고, 다른 클라이언트(VS Code 등)까지 타임아웃한다. 실제로 겪었다.
        pipeline = (
            f'type "{pub}" | ssh -o StrictHostKeyChecking=accept-new'
            f' -o NumberOfPasswordPrompts=1 -o ConnectTimeout=15 "{host}" "{inner}"'
        )
        # cmd 의 ( ) 그룹을 쓰지 않고, echo 문구에 호스트명을 넣지 않는다.
        # 별칭에 괄호가 있으면(예: my-host(dev)) 그룹이 조기 종료돼
        # "키 was unexpected at this time." 로 콘솔이 즉시 닫힌다.
        # ssh 인자의 호스트는 따옴표 안이라 안전하다.
        note = (
            "" if is_posix else
            " & echo. & echo [!] 원격 계정이 관리자면 Windows OpenSSH 는"
            " C:\\ProgramData\\ssh\\administrators_authorized_keys 를 대신 봅니다."
        )
        cmdline = (
            f'cmd /c {pipeline}'
            f' && echo [OK] 키 등록 완료'
            f' || echo [X] 실패'
            f'{note}'
            f' & echo. & pause'
        )
        try:
            subprocess.Popen(
                cmdline,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            self._show_toast(f"{host}: 콘솔에서 비밀번호를 입력하세요.")
        except Exception as e:
            messagebox.showerror("실행 실패", str(e), parent=self.window)

    def _refresh_remote_status(self):
        """2초 주기로 리스너·터널·감사 로그를 갱신한다."""
        # 창이 이미 닫혔는데 콜백이 남아 실행되면 TclError 가 난다.
        try:
            if not self.window.winfo_exists():
                return
        except Exception:
            return
        try:
            port = int(self._remote_port_var.get() or 0)
        except Exception:
            port = 0
        self._remote_listener_var.set(
            f"● :{port} LISTENING" if port and _is_port_listening(port) else "○ 미기동"
        )

        states = {}
        if self._tunnels:
            try:
                states = self._tunnels.status()
            except Exception:
                states = {}
        # 설정에 없어도 실제로 살아 있는 터널은 반드시 보여준다.
        # 열려 있는데 화면에 없는 상태를 만들지 않는 것이 이 탭의 요점이다.
        orphans = [h for h in states if h not in self._tunnel_rows]
        if orphans:
            self._tunnel_rows.extend(orphans)
            self._redraw_tunnel_tree()

        for host in self._tunnel_rows:
            if not self._tunnel_tree.exists(host):
                continue
            self._tunnel_tree.set(host, "state", _format_tunnel_state(states.get(host)))

        self._audit_var.set(_audit_tail(12))
        self._remote_after_id = self.window.after(2000, self._refresh_remote_status)

    def _build_global_tab(self, PAD: dict):
        f = self._tab_global

        # ── 자동 시작 ──
        ttk.Label(f, text="시스템 설정", font=("", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 2))
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

        ttk.Checkbutton(
            f,
            text="대시보드 자동 실행",
            variable=self._dashboard_enabled_var,
        ).grid(row=3, column=0, sticky="w", padx=16, pady=(2, 0))
        ttk.Button(
            f,
            text="대시보드 보기",
            command=lambda: webbrowser.open("http://localhost:8501"),
        ).grid(row=3, column=1, sticky="e", padx=16, pady=(2, 0))
        ttk.Label(
            f,
            text="활성화하면 overlay와 함께 로컬 대시보드 sidecar를 실행합니다.",
            foreground="gray",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 8))

        ttk.Separator(f, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        # ── 자동 컨텍스트 주입 ──
        ttk.Label(f, text="세션 설정", font=("", 9, "bold")).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 2))
        ttk.Checkbutton(
            f,
            text="CLI 공급자 시작 시 자동으로 engram 컨텍스트 주입",
            variable=self._auto_inject_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        warn_frame = tk.Frame(f, bd=1, relief="solid", bg="#fff8e1")
        warn_frame.grid(row=8, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 8))
        tk.Label(
            warn_frame,
            text=(
                "⚠  활성화하면 말풍선·터미널 세션은 물론, 전역 hook 을 통해 데스크톱 앱/CLI 등\n"
                "    임의 지점에서 시작된 claude 세션에도 engram_get_context 가 자동 호출됩니다.\n"
                "    (~/.claude/settings.json 의 SessionStart hook 을 자동 등록/해제)\n"
                "    세션 시작마다 초기 컨텍스트 토큰이 추가로 소모됩니다."
            ),
            bg="#fff8e1",
            anchor="w",
            justify="left",
            foreground="#7a5800",
        ).pack(fill="x", padx=8, pady=6)

        ttk.Separator(f, orient="horizontal").grid(row=9, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        ttk.Label(f, text="튜토리얼", font=("", 9, "bold")).grid(row=10, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 2))
        ttk.Label(
            f,
            text="튜토리얼 진행 플래그를 초기 상태로 되돌립니다. 기존 메모리/위키 데이터는 삭제되지 않습니다.",
            foreground="gray",
        ).grid(row=11, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 4))
        ttk.Button(
            f,
            text="튜토리얼 플래그 초기화",
            command=self._reset_tutorial_flags,
        ).grid(row=12, column=0, sticky="w", padx=16, pady=(0, 8))

        f.columnconfigure(1, weight=1)

    def _reset_tutorial_flags(self):
        ok = messagebox.askyesno(
            "튜토리얼 초기화",
            (
                "튜토리얼 진행 플래그를 초기화할까요?\n\n"
                "- 진행 단계/완료 단계가 초기 상태로 되돌아갑니다.\n"
                "- 기존 메모리, 위키, 세션 데이터는 유지됩니다."
            ),
            parent=self.window,
        )
        if not ok:
            return

        reset_tutorial_state(reason="manual_from_settings")
        self._show_toast("튜토리얼 플래그를 초기화했습니다. 다음 실행에서 처음 단계부터 진행됩니다.")

    def _load_current_values(self):
        cfg = self._cfg

        # 오버레이 탭
        char_name = _nested_get(cfg, ["overlay", "character", "name"], "")
        self._char_path_var.set(str(char_name or ""))
        self._flip_var.set(bool(_nested_get(cfg, ["overlay", "flip_horizontal"], False)))

        height = _nested_get(cfg, ["overlay", "char_height_ratio"], 0.125)
        self._char_height_var.set(float(height))
        self._height_label.config(text=f"{float(height):.3f}")

        workdir = _nested_get(cfg, ["cli", "workdir"], "")
        if not workdir:
            workdir = str(self._engram_user_cfg.get("workdir") or "")
        self._workdir_var.set(str(workdir))

        chat_mode = normalize_chat_mode(_nested_get(cfg, ["overlay", "chat_mode"], "bubble"))
        self._chat_mode_var.set(_CHAT_MODE_VALUE_TO_DISPLAY.get(chat_mode, _CHAT_MODE_OPTIONS[0]))

        permission_level = normalize_permission_level(_nested_get(cfg, ["bubble", "permission_level"], "auto"))
        self._permission_level_var.set(_PERMISSION_LEVEL_VALUE_TO_DISPLAY.get(permission_level, _PERMISSION_LEVEL_OPTIONS[0]))
        init_cfg = _nested_get(cfg, ["bubble", "initiative"], {})
        if not isinstance(init_cfg, dict):
            init_cfg = {}
        self._initiative_enabled_var.set(bool(init_cfg.get("enabled", False)))
        self._initiative_idle_min_var.set(max(1, round(int(init_cfg.get("idle_min_sec", 600) or 600) / 60)))
        self._initiative_gap_min_var.set(max(1, round(int(init_cfg.get("min_gap_sec", 1800) or 1800) / 60)))
        qs = int(init_cfg.get("quiet_start_hour", 22)) % 24
        qe = int(init_cfg.get("quiet_end_hour", 8)) % 24
        self._initiative_quiet_on_var.set(qs != qe)  # start==end 면 조용시간 없음
        self._initiative_quiet_start_var.set(qs if qs != qe else 22)
        self._initiative_quiet_end_var.set(qe if qs != qe else 8)
        self._initiative_phrasing_var.set(bool(init_cfg.get("phrasing", True)))

        # CLI 탭
        provider = normalize_cli_provider(_nested_get(cfg, ["cli", "provider"], "copilot"))
        self._provider_var.set(_PROVIDER_VALUE_TO_DISPLAY.get(provider, provider))

        ollama_model = get_ollama_model(cfg)
        self._ollama_model_var.set(ollama_model)
        if self._on_get_ollama_models:
            models = self._on_get_ollama_models()
            self._ollama_model_combo["values"] = models

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

        # 말풍선 탭 — 글꼴/폰트 크기
        self._bubble_font_var.set(_nested_get(cfg, ["bubble", "font_family"], "Noto Sans KR Medium"))
        self._bubble_font_size_var.set(int(_nested_get(cfg, ["bubble", "font_size"], 0) or 0))

        # 말풍선 탭 — 최대 높이 비율
        self._speech_max_h_var.set(float(_nested_get(cfg, ["bubble", "speech_max_height_ratio"], 0.55) or 0))
        self._thought_max_h_var.set(float(_nested_get(cfg, ["bubble", "thought_max_height_ratio"], 0.30) or 0))

        thought_detail = str(_nested_get(cfg, ["bubble", "thought_detail"], "full") or "full").strip().lower()
        self._thought_detail_var.set(
            _THOUGHT_DETAIL_VALUE_TO_DISPLAY.get(thought_detail, _THOUGHT_DETAIL_VALUE_TO_DISPLAY["full"])
        )

        # 말풍선 탭 — 자동 페이드아웃 (ms → 초 표시)
        for key, dflt_on, dflt_ms in (("echo", True, 8000), ("speech", True, 20000), ("thought", True, 0)):
            self._fade_vars[key].set(bool(_nested_get(cfg, ["bubble", f"{key}_fade"], dflt_on)))
            ms = int(_nested_get(cfg, ["bubble", f"{key}_dwell_ms"], dflt_ms) or 0)
            self._fade_secs[key].set(round(ms / 1000, 1))

        # 말풍선 탭 — 색상 테마
        # DEFAULT_THEME에 키가 늘어도 UI 행이 아직 없을 수 있다. 예전엔 여기서
        # KeyError가 나면서 아래 _load_persona_values()까지 통째로 건너뛰어
        # 페르소나 탭이 빈 채로 뜨는 사고가 있었다 — 없는 키는 조용히 건너뛴다.
        for key, default in shapes.DEFAULT_THEME.items():
            var = self._theme_vars.get(key)
            if var is None:
                continue
            value = _nested_get(cfg, ["bubble", "theme", key], default)
            var.set(str(value))
            self._update_theme_swatch(key)
        self._update_font_preview()  # 로드된 글꼴/크기/색으로 미리보기 초기화

        # 전역 탭
        self._autostart_var.set(_is_autostart_enabled())
        auto_inject = bool(self._engram_user_cfg.get("session", {}).get("auto_inject", False))
        self._auto_inject_var.set(auto_inject)
        self._dashboard_enabled_var.set(bool(_nested_get(cfg, ["dashboard", "enabled"], True)))

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
        # pin 여부와 무관하게 현재 슬라이더/입력값을 DB baseline에도 반영해 즉시 체감 가능하게 한다.
        baseline_updates = {**numeric_values}
        for key in ("voice", "traits", "quirks", "values", "fewshot"):
            if key in persona_values:
                baseline_updates[key] = persona_values[key]
        try:
            set_persona_baseline(baseline_updates)
        except Exception:
            pass
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
            try:
                if has_user_persona_override():
                    complete_tutorial_step("persona_setup", source="settings_save")
            except Exception:
                pass
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

        flip_on = bool(self._flip_var.get())
        _nested_set(user, ["overlay", "flip_horizontal"], True if flip_on else None)

        height = round(self._char_height_var.get(), 3)
        default_height = _nested_get(self._cfg, ["overlay", "char_height_ratio"], 0.125)
        if abs(height - float(default_height)) > 0.001:
            _nested_set(user, ["overlay", "char_height_ratio"], height)

        workdir = self._workdir_var.get().strip()
        _nested_set(user, ["cli", "workdir"], workdir or None)

        chat_mode_display = self._chat_mode_var.get().strip()
        chat_mode = _CHAT_MODE_DISPLAY_TO_VALUE.get(chat_mode_display, "bubble")
        _nested_set(user, ["overlay", "chat_mode"], None if chat_mode == "bubble" else chat_mode)

        permission_level_display = self._permission_level_var.get().strip()
        permission_level = _PERMISSION_LEVEL_DISPLAY_TO_VALUE.get(permission_level_display, "auto")
        _nested_set(user, ["bubble", "permission_level"], None if permission_level == "auto" else permission_level)

        # 능동 발화 — 기본값과 같은 항목은 사용자 yaml 을 어지럽히지 않게 저장하지 않는다(None → 제거).
        initiative_on = bool(self._initiative_enabled_var.get())
        _nested_set(user, ["bubble", "initiative", "enabled"], True if initiative_on else None)

        idle_sec = max(1, int(self._initiative_idle_min_var.get() or 10)) * 60
        _nested_set(user, ["bubble", "initiative", "idle_min_sec"], None if idle_sec == 600 else idle_sec)

        gap_sec = max(1, int(self._initiative_gap_min_var.get() or 30)) * 60
        _nested_set(user, ["bubble", "initiative", "min_gap_sec"], None if gap_sec == 1800 else gap_sec)

        if self._initiative_quiet_on_var.get():
            qs = int(self._initiative_quiet_start_var.get()) % 24
            qe = int(self._initiative_quiet_end_var.get()) % 24
            if qs == qe:
                qe = (qs + 1) % 24  # start==end 는 "조용시간 없음"이므로, 켠 상태에선 최소 1시간 확보
        else:
            qs = qe = 0  # start==end → 조용시간 없음
        _nested_set(user, ["bubble", "initiative", "quiet_start_hour"], None if qs == 22 else qs)
        _nested_set(user, ["bubble", "initiative", "quiet_end_hour"], None if qe == 8 else qe)

        phrasing_on = bool(self._initiative_phrasing_var.get())
        _nested_set(user, ["bubble", "initiative", "phrasing"], None if phrasing_on else False)

        # ── CLI 탭 ──
        provider = self._provider_var.get().strip()
        provider_value = _PROVIDER_DISPLAY_TO_VALUE.get(provider, provider)
        if provider_value:
            _nested_set(user, ["cli", "provider"], normalize_cli_provider(provider_value))

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

        # ── 말풍선 탭 — 글꼴/폰트 크기 (기본값이면 저장 안 함) ──
        fam = (self._bubble_font_var.get() or "").strip()
        _nested_set(user, ["bubble", "font_family"], None if (not fam or fam == "Noto Sans KR Medium") else fam)
        try:
            fsize = int(self._bubble_font_size_var.get())
        except (tk.TclError, ValueError):
            fsize = 0
        _nested_set(user, ["bubble", "font_size"], None if fsize <= 0 else fsize)

        # ── 말풍선 탭 — 최대 높이 비율 ──
        for attr, key, default in (
            ("_speech_max_h_var", "speech_max_height_ratio", 0.55),
            ("_thought_max_h_var", "thought_max_height_ratio", 0.30),
        ):
            try:
                val = round(float(getattr(self, attr).get()), 2)
            except (tk.TclError, ValueError):
                val = default
            _nested_set(user, ["bubble", key], None if val == default else (val if val > 0 else 0))

        # ── 말풍선 탭 — 생각 풍선 내용 (기본값 full 이면 저장 안 함) ──
        thought_detail = _THOUGHT_DETAIL_DISPLAY_TO_VALUE.get(self._thought_detail_var.get().strip(), "full")
        _nested_set(user, ["bubble", "thought_detail"], None if thought_detail == "full" else thought_detail)

        # ── 말풍선 탭 — 자동 페이드아웃 (기본값이면 저장 안 함) ──
        for key, dflt_on, dflt_ms in (("echo", True, 8000), ("speech", True, 20000), ("thought", True, 0)):
            on = bool(self._fade_vars[key].get())
            _nested_set(user, ["bubble", f"{key}_fade"], None if on == dflt_on else on)
            try:
                ms = int(round(float(self._fade_secs[key].get()) * 1000))
            except (tk.TclError, ValueError):
                ms = dflt_ms
            _nested_set(user, ["bubble", f"{key}_dwell_ms"], None if ms == dflt_ms else ms)

        # ── 말풍선 탭 — 색상 테마 (기본값과 같으면 저장 안 함) ──
        for key, default in shapes.DEFAULT_THEME.items():
            var = self._theme_vars.get(key)
            if var is None:
                continue  # UI 행이 없는 키 — 사용자 설정값도 없으므로 건드리지 않는다
            value = var.get().strip()
            _nested_set(user, ["bubble", "theme", key], None if (not value or value == default) else value)

        # ── 원격 탭 ──
        remote_on = bool(self._remote_enabled_var.get())
        _nested_set(user, ["mcp", "remote_enabled"], True if remote_on else None)
        try:
            rport = int(self._remote_port_var.get())
        except (tk.TclError, ValueError):
            rport = 17386
        _nested_set(user, ["mcp", "remote_port"], None if rport == 17386 else rport)
        tunnels = [{"host": h} for h in self._tunnel_rows]
        _nested_set(user, ["mcp", "tunnels"], tunnels or None)
        auto_rc = bool(self._tunnel_autoreconnect_var.get())
        _nested_set(user, ["mcp", "tunnel_auto_reconnect"], True if auto_rc else None)

        dashboard_enabled = bool(self._dashboard_enabled_var.get())
        _nested_set(user, ["dashboard", "enabled"], None if dashboard_enabled else False)

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
        # auto_inject 토글 즉시 반영 — 전역 SessionStart hook 설치/제거(멱등).
        # runtime_config 캐시가 예전 값을 들고 있을 수 있어 방금 저장한 값으로 직접 동기화한다.
        try:
            from core.integrations.engram_bootstrap import sync_sessionstart_hook

            sync_sessionstart_hook(auto_inject)
        except Exception:
            pass

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
