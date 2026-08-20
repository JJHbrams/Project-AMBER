"""overlay 설정 GUI — tkinter 기반 설정 다이얼로그.

오버레이 우클릭 컨텍스트 메뉴 또는 트레이 아이콘 → '설정'을 누르면 열림.
변경한 값만 ~/.engram/overlay.user.yaml 에 저장한다.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox
from typing import Callable

import yaml
from PIL import Image, ImageGrab

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
    resolve_editable_overlay_path,
    resolve_path,
)
from overlay.character_assets import (
    USER_REACTION_PACKS_DIR,
    normalize_sprite_transform,
    normalize_sprite_vfx,
    resolve_bundled_character_source,
    resolve_bundled_reaction_sheet,
    resolve_reaction_pack,
)
from overlay.cli_capabilities import effort_key, efforts as provider_efforts, model_key, models as provider_models, validate as validate_cli
from core.identity import get_persona_db_baseline, set_persona_baseline
from core.config.runtime_config import normalize_policy_guidance_level
from core.tutorial import complete_tutorial_step, has_user_persona_override, reset_tutorial_state

_PROVIDER_OPTIONS = [
    "copilot",
    "gemini",
    "codex",
    "claude-code",
    "claude-code(ollama)",
    "ollama",
]
_PROVIDER_DISPLAY_TO_VALUE = {
    "copilot": "copilot",
    "gemini": "gemini",
    "codex": "codex",
    "claude-code": "claude-code",
    "claude-code(ollama)": "claude-code-ollama",
    "ollama": "ollama",
}
_PROVIDER_VALUE_TO_DISPLAY = {
    "copilot": "copilot",
    "gemini": "gemini",
    "codex": "codex",
    "claude-code": "claude-code",
    "claude-code-ollama": "claude-code(ollama)",
    "ollama": "ollama",
}
_POLICY_LEVEL_OPTIONS = ["끔", "경고만", "Agent 강제 · 사람 경고 (권장)"]
_POLICY_LEVEL_DISPLAY_TO_VALUE = {
    "끔": "off",
    "경고만": "warn",
    "Agent 강제 · 사람 경고 (권장)": "enforce_agents",
}
OVERLAY_EVENT_API_MANUAL_URL = "http://localhost:8501/?page=manual&manual=overlay-event-api"


def open_overlay_event_api_manual(opener: Callable[[str], object] | None = None) -> object:
    """Open the installed manual page; injected opener keeps this UI action testable."""
    opener = opener or webbrowser.open
    return opener(OVERLAY_EVENT_API_MANUAL_URL)
_POLICY_LEVEL_VALUE_TO_DISPLAY = {
    value: display for display, value in _POLICY_LEVEL_DISPLAY_TO_VALUE.items()
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

_CHARACTER_SOURCE_MODE_DISPLAY_TO_VALUE = {
    "스프라이트 그리드": "sprite_grid",
    "단일 이미지": "static",
    "애니메이션 폴더": "sequence",
}
_CHARACTER_SOURCE_MODE_VALUE_TO_DISPLAY = {
    value: display for display, value in _CHARACTER_SOURCE_MODE_DISPLAY_TO_VALUE.items()
}
_CHARACTER_SOURCE_MODE_OPTIONS = list(_CHARACTER_SOURCE_MODE_DISPLAY_TO_VALUE)

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


def character_height_override_value(height: float, base_height: float) -> float | None:
    """Keep only values that differ from the distributable base setting."""
    return height if abs(height - base_height) > 0.001 else None


def character_source_mode_to_display(value: object) -> str:
    """Return the localized GUI label for a persisted character source mode."""
    return _CHARACTER_SOURCE_MODE_VALUE_TO_DISPLAY.get(str(value or "").strip(), "단일 이미지")


def character_source_mode_from_display(value: object) -> str:
    """Return the persisted source mode for a localized GUI label."""
    return _CHARACTER_SOURCE_MODE_DISPLAY_TO_VALUE.get(str(value or "").strip(), "static")


def _resolve_character_source_path(value: object, source_mode: str = "") -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if path.is_absolute():
        if source_mode == "sprite_grid":
            return resolve_bundled_reaction_sheet(value) or path
        return resolve_bundled_character_source(value, source_mode) or path
    if source_mode == "sprite_grid":
        reaction_sheet = resolve_bundled_reaction_sheet(value)
        if reaction_sheet is not None:
            return reaction_sheet
    bundled = resolve_bundled_character_source(value, source_mode)
    return bundled if bundled is not None else resolve_path(str(path))


def validate_sprite_grid(
    path: object,
    columns: object,
    rows: object,
    cell_width: object,
    cell_height: object,
    chroma_key: object,
) -> tuple[bool, str]:
    """Validate a user-selected sprite-grid asset without touching configuration files."""
    try:
        parsed_columns, parsed_rows = int(columns), int(rows)
        parsed_width, parsed_height = int(cell_width), int(cell_height)
        chroma = str(chroma_key or "").strip()
    except (TypeError, ValueError):
        return False, "열·행·셀 크기는 양의 정수여야 합니다."
    if min(parsed_columns, parsed_rows, parsed_width, parsed_height) <= 0:
        return False, "열·행·셀 크기는 양의 정수여야 합니다."
    if len(chroma) != 7 or not chroma.startswith("#"):
        return False, "chroma는 #RRGGBB 형식이어야 합니다."
    try:
        int(chroma[1:], 16)
    except ValueError:
        return False, "chroma는 #RRGGBB 형식이어야 합니다."
    image_path = _resolve_character_source_path(path, "sprite_grid")
    if not image_path.is_file() or image_path.suffix.lower() != ".png":
        return False, "스프라이트 그리드는 PNG 파일을 선택해야 합니다."
    try:
        with Image.open(image_path) as image:
            actual_size = image.size
    except (OSError, ValueError):
        return False, "스프라이트 PNG를 읽을 수 없습니다."
    expected_size = (parsed_columns * parsed_width, parsed_rows * parsed_height)
    if actual_size != expected_size:
        return False, f"이미지 {actual_size[0]}×{actual_size[1]}와 grid {expected_size[0]}×{expected_size[1]}가 일치하지 않습니다."
    return True, "유효"


def validate_character_source(mode: object, character_path: object, grid_values: tuple[object, object, object, object, object, object]) -> tuple[bool, str]:
    """Validate the active character-source mode before a settings write."""
    normalized_mode = str(mode or "").strip()
    if normalized_mode == "sprite_grid":
        return validate_sprite_grid(*grid_values)
    source_path = _resolve_character_source_path(character_path, normalized_mode)
    if normalized_mode == "static":
        if source_path.is_file() and source_path.suffix.lower() == ".png":
            return True, "유효"
        return False, "단일 이미지 모드는 PNG 파일을 선택해야 합니다."
    if normalized_mode == "sequence":
        if source_path.is_dir():
            return True, "유효"
        return False, "애니메이션 폴더 모드는 폴더를 선택해야 합니다."
    return False, "알 수 없는 캐릭터 소스 모드입니다."


_MANIFEST_SELECTIONS = {"random", "sequence", "sequence_once", "fixed", "shuffle"}
_MANIFEST_TRANSFORM_OPTIONS = {
    "없음": "none",
    "숨쉬기 + 무작위 좌우 반전": "breathe_mirror",
    "좌우 반전 + 세로 squash": "hflip_squash",
}
_MANIFEST_VFX_OPTIONS = {
    "없음": "none",
    "은은한 반짝임 (twinkle)": "twinkle",
    "반짝임 폭발 (sparkle burst)": "sparkle_burst",
}


def manifest_transform_to_display(value: object) -> str:
    canonical = normalize_sprite_transform(value)
    return next((label for label, name in _MANIFEST_TRANSFORM_OPTIONS.items() if name == canonical), str(value))


def manifest_transform_from_display(value: object) -> str | None:
    return _MANIFEST_TRANSFORM_OPTIONS.get(str(value), normalize_sprite_transform(value))


def manifest_vfx_to_display(value: object) -> str:
    canonical = normalize_sprite_vfx(value)
    return next((label for label, name in _MANIFEST_VFX_OPTIONS.items() if name == canonical), str(value))


def manifest_vfx_from_display(value: object) -> str | None:
    return _MANIFEST_VFX_OPTIONS.get(str(value), normalize_sprite_vfx(value))


def validate_manifest_state(state: dict, cell_count: int) -> tuple[bool, str]:
    """Validate editable state fields while leaving unrelated YAML intact."""
    try:
        frames = [int(part.strip()) for part in str(state.get("frames", "")).split(",") if part.strip()]
        frame_ms = int(state.get("frame_ms"))
        dwell_raw = str(state.get("dwell_ms", "")).strip()
        dwell_ms = int(dwell_raw) if dwell_raw else None
    except (TypeError, ValueError):
        return False, "frames와 timing은 정수여야 합니다."
    if not frames or any(index < 0 or index >= cell_count for index in frames):
        return False, f"frames는 0~{max(0, cell_count - 1)} 범위여야 합니다."
    if frame_ms <= 0 or (dwell_ms is not None and dwell_ms <= 0):
        return False, "timing은 양수여야 합니다."
    if (
        state.get("selection") not in _MANIFEST_SELECTIONS
        or manifest_transform_from_display(state.get("transform")) is None
        or manifest_vfx_from_display(state.get("vfx")) is None
    ):
        return False, "selection, transform 또는 vfx 값이 허용 범위가 아닙니다."
    return True, "유효"


def ensure_user_reaction_pack(pack_id: object) -> Path:
    """Make a writable user copy of the active pack; never edit bundled assets."""
    safe_id = str(pack_id or "").strip()
    if not safe_id or not all(char.isalnum() or char in "_-" for char in safe_id):
        raise ValueError("유효한 reaction pack id가 없습니다.")
    target = USER_REACTION_PACKS_DIR / safe_id
    manifest = target / "manifest.yaml"
    if manifest.is_file():
        return target
    if target.exists():
        raise ValueError(f"사용자 pack 디렉토리에 manifest.yaml이 없습니다: {target}")
    resolved = resolve_reaction_pack(safe_id)
    if resolved.source == "disabled" or resolved.sprite_sheet is None:
        raise ValueError("활성 reaction pack을 찾을 수 없습니다.")
    source = resolved.sprite_sheet.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{safe_id}-", dir=str(target.parent)))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if not manifest.is_file():
        raise ValueError("reaction pack 복사 후 manifest.yaml을 찾을 수 없습니다.")
    return target


def save_reaction_manifest(pack_id: object, raw: dict) -> Path:
    root = ensure_user_reaction_pack(pack_id)
    manifest = root / "manifest.yaml"
    temporary = manifest.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temporary, manifest)
    return manifest


def open_reaction_manifest(pack_id: object) -> Path:
    manifest = ensure_user_reaction_pack(pack_id) / "manifest.yaml"
    os.startfile(str(manifest))
    return manifest


def rgb_to_hex(color: tuple[int, ...]) -> str:
    """Convert an RGB/RGBA pixel into the canonical settings color format."""
    red, green, blue = (max(0, min(255, int(channel))) for channel in color[:3])
    return f"#{red:02X}{green:02X}{blue:02X}"


def sample_snapshot_color(snapshot: Image.Image, screen_x: int, screen_y: int, origin: tuple[int, int]) -> str | None:
    """Read one screen coordinate from a pre-overlay screenshot, or return None."""
    image_x, image_y = int(screen_x) - origin[0], int(screen_y) - origin[1]
    if not (0 <= image_x < snapshot.width and 0 <= image_y < snapshot.height):
        return None
    pixel = snapshot.convert("RGB").getpixel((image_x, image_y))
    return rgb_to_hex(pixel)


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
        self._persona_numeric_overwrite_btns: dict[str, ttk.Button] = {}
        self._persona_db_baselines: dict[str, float] = {}
        self._persona_load_ok = False
        self._persona_banner_var = tk.StringVar(value="현재 기본 페르소나가 적용되어 있습니다. 커스텀 페르소나를 적용해 보세요.")
        self._autostart_var = tk.BooleanVar()
        self._auto_inject_var = tk.BooleanVar()
        self._policy_level_var = tk.StringVar(value="경고만")
        self._policy_status_var = tk.StringVar(value="저장 후 Claude/Codex 적용 상태가 표시됩니다.")
        self._policy_sync_warnings: list[str] = []
        self._dashboard_enabled_var = tk.BooleanVar()
        self._external_daily_dir_var = tk.StringVar()
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

        self._persona_tip_frame = tk.Frame(self.window, bd=1, relief="solid", bg="#f4f6e1")
        tip_frame = self._persona_tip_frame
        tk.Label(
            tip_frame,
            textvariable=self._persona_banner_var,
            bg="#f4f6e1",
            anchor="w",
            justify="left",
        ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        ttk.Button(tip_frame, text="페르소나 열기", command=self._open_persona_file).pack(side="right", padx=6, pady=4)

        self._settings_notebook = ttk.Notebook(self.window)
        notebook = self._settings_notebook
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
        notebook.bind("<<NotebookTabChanged>>", self._sync_persona_tip_visibility)

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
        self._sync_persona_tip_visibility()

    def _sync_persona_tip_visibility(self, _event=None) -> None:
        """Keep the persona shortcut in its own tab, without changing its safety behavior."""
        selected = self._settings_notebook.nametowidget(self._settings_notebook.select())
        if selected is self._tab_persona:
            if not self._persona_tip_frame.winfo_manager():
                self._persona_tip_frame.pack(fill="x", padx=10, pady=(10, 0), before=self._settings_notebook)
        elif self._persona_tip_frame.winfo_manager():
            self._persona_tip_frame.pack_forget()

    def _close(self):
        self._cancel_grid_eyedropper()
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

        self._custom_overlay_help_label = ttk.Label(
            f, text="커스텀 오버레이 적용 방법", foreground="gray"
        )
        self._custom_overlay_help_label.grid(row=0, column=0, columnspan=3, sticky="e", padx=(8, 2), pady=(5, 0))
        self._custom_overlay_help_button = ttk.Button(
            f, text="?", width=3, command=open_overlay_event_api_manual
        )
        self._custom_overlay_help_button.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=(5, 0))

        # 캐릭터 소스 — 서로 배타적인 세 모드를 한 프레임에 모아 이후 설정 행과 겹치지 않게 둔다.
        source_box = ttk.LabelFrame(f, text="캐릭터 소스")
        source_box.grid(row=1, column=0, columnspan=5, sticky="ew", padx=8, pady=(4, 4))
        # Keep the growing path column bounded so the browse controls never leave a
        # normal 850–950px settings window.  Grid fields below are intentionally
        # split across rows instead of determining this frame's requested width.
        source_box.columnconfigure(1, weight=1, minsize=250)
        source_box.columnconfigure(2, weight=0, minsize=116)
        self._char_source_mode_var = tk.StringVar()
        self._char_source_mode_combo = ttk.Combobox(
            source_box, textvariable=self._char_source_mode_var,
            values=_CHARACTER_SOURCE_MODE_OPTIONS, state="readonly", width=16,
        )
        ttk.Label(source_box, text="방식:").grid(row=0, column=0, sticky="w", **PAD)
        self._char_source_mode_combo.grid(row=0, column=1, sticky="w", **PAD)
        self._char_path_var = tk.StringVar()
        ttk.Label(source_box, text="이미지 / 폴더:").grid(row=1, column=0, sticky="w", **PAD)
        self._char_path_entry = ttk.Entry(source_box, textvariable=self._char_path_var, width=28)
        self._char_path_entry.grid(row=1, column=1, sticky="ew", **PAD)
        btn_frame_char = ttk.Frame(source_box)
        btn_frame_char.grid(row=1, column=2, sticky="w", padx=(0, 4), pady=4)
        self._char_file_button = ttk.Button(btn_frame_char, text="파일...", width=7, command=self._browse_char_file)
        self._char_file_button.pack(side="left", padx=(0, 2))
        self._char_dir_button = ttk.Button(btn_frame_char, text="폴더...", width=7, command=self._browse_char_dir)
        self._char_dir_button.pack(side="left")
        self._flip_var = tk.BooleanVar()
        ttk.Checkbutton(source_box, text="기본 좌우 반전", variable=self._flip_var).grid(row=1, column=3, sticky="w", padx=(4, 8), pady=4)
        self._legacy_body_motion_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            source_box,
            text="단일 이미지 레거시 움직임 (늘림/상하 이동)",
            variable=self._legacy_body_motion_var,
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=(4, 8), pady=4)
        self._grid_path_var = tk.StringVar()
        self._grid_columns_var, self._grid_rows_var = tk.StringVar(), tk.StringVar()
        self._grid_cell_width_var, self._grid_cell_height_var, self._grid_chroma_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self._grid_status_var = tk.StringVar()
        ttk.Label(source_box, text="스프라이트 PNG:").grid(row=2, column=0, sticky="w", **PAD)
        self._grid_path_entry = ttk.Entry(source_box, textvariable=self._grid_path_var, width=28)
        self._grid_path_entry.grid(row=2, column=1, sticky="ew", **PAD)
        self._grid_file_button = ttk.Button(source_box, text="파일...", command=self._browse_grid_file)
        self._grid_file_button.grid(row=2, column=2, sticky="w", **PAD)
        grid_frame = ttk.Frame(source_box)
        grid_frame.grid(row=3, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        ttk.Label(grid_frame, text="열:").grid(row=0, column=0, sticky="w")
        self._grid_columns_input = ttk.Spinbox(grid_frame, textvariable=self._grid_columns_var, from_=1, to=99, width=5)
        self._grid_columns_input.grid(row=0, column=1, padx=(2, 8))
        ttk.Label(grid_frame, text="행:").grid(row=0, column=2, sticky="w")
        self._grid_rows_input = ttk.Spinbox(grid_frame, textvariable=self._grid_rows_var, from_=1, to=99, width=5)
        self._grid_rows_input.grid(row=0, column=3, padx=(2, 8))
        ttk.Label(grid_frame, text="셀 너비:").grid(row=0, column=4, sticky="w")
        self._grid_cell_width_input = ttk.Spinbox(grid_frame, textvariable=self._grid_cell_width_var, from_=1, to=9999, width=6)
        self._grid_cell_width_input.grid(row=0, column=5, padx=(2, 8))
        ttk.Label(grid_frame, text="셀 높이:").grid(row=0, column=6, sticky="w")
        self._grid_cell_height_input = ttk.Spinbox(grid_frame, textvariable=self._grid_cell_height_var, from_=1, to=9999, width=6)
        self._grid_cell_height_input.grid(row=0, column=7, padx=(2, 8))
        ttk.Label(source_box, text="Chroma:").grid(row=4, column=0, sticky="w", **PAD)
        self._grid_chroma_entry = ttk.Entry(source_box, textvariable=self._grid_chroma_var, width=10)
        self._grid_chroma_entry.grid(row=4, column=1, sticky="w", **PAD)
        # Match the bubble-theme color UX: click the swatch for the OS picker
        # (including its platform eyedropper where available), or type #RRGGBB.
        self._grid_chroma_swatch = tk.Button(
            source_box, width=10, command=self._pick_grid_chroma_color,
        )
        self._grid_chroma_swatch.grid(row=4, column=2, sticky="w", **PAD)
        self._grid_eyedropper_button = ttk.Button(
            source_box, text="스포이트", command=self._start_grid_eyedropper,
        )
        self._grid_eyedropper_button.grid(row=4, column=3, sticky="w", **PAD)
        ttk.Label(source_box, textvariable=self._grid_status_var, foreground="gray").grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        self._grid_controls = (
            self._grid_path_entry, self._grid_file_button, self._grid_columns_input,
            self._grid_rows_input, self._grid_cell_width_input, self._grid_cell_height_input,
            self._grid_chroma_entry, self._grid_chroma_swatch, self._grid_eyedropper_button,
        )
        self._manifest_box = ttk.LabelFrame(source_box, text="Sprite state manifest (사용자 팩으로 저장)")
        self._manifest_box.grid(row=6, column=0, columnspan=4, sticky="ew", padx=8, pady=(5, 8))
        self._manifest_box.columnconfigure(1, weight=1)
        self._manifest_state_var = tk.StringVar()
        self._manifest_frames_var = tk.StringVar()
        self._manifest_selection_var = tk.StringVar(value="fixed")
        self._manifest_frame_ms_var = tk.StringVar()
        self._manifest_dwell_ms_var = tk.StringVar()
        self._manifest_transform_var = tk.StringVar(value="none")
        self._manifest_vfx_var = tk.StringVar(value="none")
        self._manifest_status_var = tk.StringVar(value="sprite grid에서 활성화됩니다.")
        ttk.Label(self._manifest_box, text="State:").grid(row=0, column=0, padx=4, pady=3, sticky="w")
        self._manifest_state_combo = ttk.Combobox(self._manifest_box, textvariable=self._manifest_state_var, state="readonly", width=15)
        self._manifest_state_combo.grid(row=0, column=1, padx=4, pady=3, sticky="w")
        ttk.Label(self._manifest_box, text="frames:").grid(row=1, column=0, padx=4, pady=3, sticky="w")
        self._manifest_frames_entry = ttk.Entry(self._manifest_box, textvariable=self._manifest_frames_var, width=22)
        self._manifest_frames_entry.grid(row=1, column=1, padx=4, pady=3, sticky="w")
        self._manifest_selection_combo = ttk.Combobox(self._manifest_box, textvariable=self._manifest_selection_var, values=tuple(_MANIFEST_SELECTIONS), state="readonly", width=10)
        self._manifest_transform_combo = ttk.Combobox(self._manifest_box, textvariable=self._manifest_transform_var, values=tuple(_MANIFEST_TRANSFORM_OPTIONS), state="readonly", width=24)
        self._manifest_vfx_combo = ttk.Combobox(self._manifest_box, textvariable=self._manifest_vfx_var, values=tuple(_MANIFEST_VFX_OPTIONS), state="readonly", width=14)
        for column, (label, combo) in enumerate((("selection", self._manifest_selection_combo), ("transform", self._manifest_transform_combo), ("vfx", self._manifest_vfx_combo))):
            ttk.Label(self._manifest_box, text=label).grid(row=2, column=column * 2, padx=3, pady=3)
            combo.grid(row=2, column=column * 2 + 1, padx=3, pady=3)
        ttk.Label(self._manifest_box, text="frame/dwell ms:").grid(row=3, column=0, padx=3, pady=3)
        self._manifest_frame_ms_entry = ttk.Entry(self._manifest_box, textvariable=self._manifest_frame_ms_var, width=8)
        self._manifest_frame_ms_entry.grid(row=3, column=1, padx=3, pady=3)
        self._manifest_dwell_ms_entry = ttk.Entry(self._manifest_box, textvariable=self._manifest_dwell_ms_var, width=8)
        self._manifest_dwell_ms_entry.grid(row=3, column=2, padx=3, pady=3)
        self._manifest_save_button = ttk.Button(self._manifest_box, text="저장", command=self._save_manifest_state)
        self._manifest_save_button.grid(row=3, column=3, padx=3, pady=3)
        self._manifest_reload_button = ttk.Button(self._manifest_box, text="다시 읽기", command=self._reload_manifest_editor)
        self._manifest_reload_button.grid(row=3, column=4, padx=3, pady=3)
        self._manifest_yaml_button = ttk.Button(self._manifest_box, text="고급 YAML", command=self._open_manifest_editor)
        self._manifest_yaml_button.grid(row=3, column=5, padx=3, pady=3)
        ttk.Label(self._manifest_box, textvariable=self._manifest_status_var, foreground="gray").grid(row=4, column=0, columnspan=6, padx=4, pady=(0, 3), sticky="w")
        self._manifest_controls = (
            self._manifest_state_combo, self._manifest_frames_entry, self._manifest_selection_combo,
            self._manifest_transform_combo, self._manifest_vfx_combo, self._manifest_frame_ms_entry,
            self._manifest_dwell_ms_entry, self._manifest_save_button, self._manifest_reload_button,
            self._manifest_yaml_button,
        )
        self._manifest_state_combo.bind("<<ComboboxSelected>>", self._manifest_select_state)
        self._char_source_mode_combo.bind("<<ComboboxSelected>>", self._on_character_source_mode_changed)
        for variable in (self._grid_path_var, self._grid_columns_var, self._grid_rows_var,
                         self._grid_cell_width_var, self._grid_cell_height_var, self._grid_chroma_var):
            variable.trace_add("write", self._on_grid_value_changed)

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
        provider_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_provider_capability_controls())

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

        ttk.Label(f, text="공급자 모델:").grid(row=5, column=0, sticky="w", **PAD)
        self._provider_model_var = tk.StringVar()
        self._provider_model_combo = ttk.Combobox(f, textvariable=self._provider_model_var, width=19)
        self._provider_model_combo.grid(row=5, column=1, sticky="ew", **PAD)
        self._provider_model_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_provider_capability_controls())
        ttk.Label(f, text="Reasoning effort:").grid(row=6, column=0, sticky="w", **PAD)
        self._provider_effort_var = tk.StringVar()
        self._provider_effort_combo = ttk.Combobox(f, textvariable=self._provider_effort_var, width=19)
        self._provider_effort_combo.grid(row=6, column=1, sticky="ew", **PAD)

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

    def _update_provider_capability_controls(self) -> None:
        provider = _PROVIDER_DISPLAY_TO_VALUE.get(self._provider_var.get(), self._provider_var.get())
        cli = (self._cfg.get("cli") or {}) if isinstance(self._cfg, dict) else {}
        available = provider_models(provider, cli, self._on_get_ollama_models() if self._on_get_ollama_models else [])
        self._provider_model_combo["values"] = available
        key = model_key(provider)
        configured_model = str(cli.get(key) or "") if key else ""
        if not self._provider_model_var.get() or self._provider_model_var.get() not in available:
            self._provider_model_var.set(configured_model)
        effort_values = provider_efforts(provider, cli, self._provider_model_var.get())
        self._provider_effort_combo["values"] = effort_values
        ekey = effort_key(provider)
        configured_effort = str(cli.get(ekey) or "") if ekey else ""
        if not self._provider_effort_var.get() or self._provider_effort_var.get() not in effort_values:
            self._provider_effort_var.set(configured_effort)
        self._provider_model_combo.configure(state="readonly" if available else "disabled")
        self._provider_effort_combo.configure(state="readonly" if effort_values else "disabled")

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
        ttk.Label(f, text="DB 반영", foreground="gray").grid(row=9, column=4, sticky="w", padx=(0, 8), pady=(2, 0))

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
            btn = ttk.Button(
                f,
                text="→ DB",
                width=6,
                command=lambda key=field: self._on_persona_overwrite(key),
            )
            btn.grid(row=row, column=4, sticky="w", padx=(0, 8), pady=4)
            btn.state(["disabled"])
            self._persona_numeric_overwrite_btns[field] = btn

        ttk.Label(
            f,
            text="pin 해제 시 DB 진화값을 따릅니다. → DB는 현재 값을 DB baseline에 영구 반영하고 pin을 해제합니다.",
            foreground="gray",
        ).grid(row=14, column=0, columnspan=5, sticky="w", padx=8, pady=(4, 8))

        f.columnconfigure(1, weight=1)

    def _on_persona_slider_changed(self, field: str, raw_value):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = float(self._persona_numeric_vars[field].get())
        self._persona_numeric_label_vars[field].set(f"{value:.2f}")

    def _on_persona_overwrite(self, field: str):
        """현재 슬라이더 값을 DB baseline에 명시적으로 반영하고 pin을 해제한다."""
        if not self._persona_load_ok or field not in self._persona_db_baselines:
            messagebox.showwarning("페르소나 로드 필요", "페르소나 DB 값을 안전하게 읽지 못했습니다. DB 반영은 차단됩니다.", parent=self.window)
            return
        try:
            value = _coerce_persona_number(self._persona_numeric_vars[field].get(), _PERSONA_DEFAULTS[field])
        except Exception:
            return

        old_value = self._persona_db_baselines[field]
        if not messagebox.askyesno(
            "DB baseline 덮어쓰기 확인",
            f"{field} DB baseline을 변경합니다.\n\n현재 DB: {old_value:.2f}\n새 값: {value:.2f}\n\n계속하시겠습니까?",
            parent=self.window,
        ):
            return
        try:
            set_persona_baseline({field: value})
        except Exception as exc:
            messagebox.showerror("DB 저장 실패", f"{field} 값을 DB에 저장하지 못했습니다.\n{exc}", parent=self.window)
            return

        # DB 값이 user YAML의 pin에 가려지지 않도록 해당 필드의 pin을 즉시 해제·저장한다.
        self._persona_db_baselines[field] = value
        self._persona_numeric_pin_vars[field].set(False)
        try:
            self._save_persona_user_file()
        except Exception as exc:
            messagebox.showwarning(
                "DB 저장 완료, 페르소나 파일 저장 실패",
                f"{field} DB baseline은 {value:.2f}로 저장되었습니다.\n"
                f"pin 해제는 persona.user.yaml에 저장되지 않았습니다.\n{exc}",
                parent=self.window,
            )
            return

        # 버튼 일시 피드백
        btn = self._persona_numeric_overwrite_btns.get(field)
        if btn:
            btn.configure(text="완료✓")
            self.window.after(1200, lambda: btn.configure(text="→ DB"))

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
            text="자동 재연결을 켜면 오버레이 시작 시 저장된 호스트를 키 인증으로 연결한다.",
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
            text="시작·끊김 시 자동 재연결",
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

        policy_level_row = ttk.Frame(f)
        policy_level_row.grid(row=9, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))
        ttk.Label(policy_level_row, text="저장소 정책 수준").pack(side="left")
        ttk.Combobox(
            policy_level_row,
            textvariable=self._policy_level_var,
            values=_POLICY_LEVEL_OPTIONS,
            state="readonly",
            width=29,
        ).pack(side="left", padx=(10, 0))
        policy_info = ttk.Frame(f)
        policy_info.grid(row=10, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 8))
        ttk.Label(
            policy_info,
            text=(
                "사람의 Git commit은 항상 경고만 표시합니다. Agent 강제 수준에서는 Claude·Codex의\n"
                "정책 위반 도구 호출만 차단하며, hook/backend 오류는 경고 후 허용합니다."
            ),
            foreground="gray",
        ).pack(anchor="w")
        ttk.Label(policy_info, textvariable=self._policy_status_var, foreground="#6a4c00").pack(anchor="w")

        ttk.Label(f, text="Obsidian Daily Note 디렉터리", font=("", 9, "bold")).grid(row=11, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 2))
        daily_frame = ttk.Frame(f)
        daily_frame.grid(row=12, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 0))
        ttk.Entry(daily_frame, textvariable=self._external_daily_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(daily_frame, text="찾기...", command=self._browse_external_daily_dir).pack(side="left", padx=(6, 0))
        ttk.Label(
            f,
            text="선택 사항입니다. 지정하면 Engram Wiki daily note와 함께 Obsidian Daily Note에도 기록합니다.",
            foreground="gray",
        ).grid(row=13, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 8))

        ttk.Separator(f, orient="horizontal").grid(row=14, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        ttk.Label(f, text="튜토리얼", font=("", 9, "bold")).grid(row=15, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 2))
        ttk.Label(
            f,
            text="튜토리얼 진행 플래그를 초기 상태로 되돌립니다. 기존 메모리/위키 데이터는 삭제되지 않습니다.",
            foreground="gray",
        ).grid(row=16, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 4))
        ttk.Button(
            f,
            text="튜토리얼 플래그 초기화",
            command=self._reset_tutorial_flags,
        ).grid(row=17, column=0, sticky="w", padx=16, pady=(0, 8))

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
        mode = _nested_get(cfg, ["overlay", "character", "source_mode"], "")
        stored_mode = str(mode or ("sequence" if Path(str(char_name or "")).is_dir() else "static"))
        self._char_source_mode_var.set(character_source_mode_to_display(stored_mode))
        grid = _nested_get(cfg, ["overlay", "character", "reactions", "grid"], {}) or {}
        self._grid_path_var.set(str(_nested_get(cfg, ["overlay", "character", "reactions", "sprite_sheet"], "")))
        self._grid_columns_var.set(str(grid.get("columns", 6)))
        self._grid_rows_var.set(str(grid.get("rows", 4)))
        self._grid_cell_width_var.set(str(grid.get("cell_width", 434)))
        self._grid_cell_height_var.set(str(grid.get("cell_height", 408)))
        self._grid_chroma_var.set(str(_nested_get(cfg, ["overlay", "character", "reactions", "chroma_key"], "#00FF00")))
        self._update_grid_status()
        self._apply_character_source_mode()
        self._reload_manifest_editor()
        self._flip_var.set(bool(_nested_get(cfg, ["overlay", "flip_horizontal"], False)))
        self._legacy_body_motion_var.set(bool(_nested_get(cfg, ["overlay", "character", "effects", "legacy_body_motion"], False)))

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
        self._update_provider_capability_controls()

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
        guidance_level = _nested_get(
            self._engram_user_cfg,
            ["directives", "policy", "guidance_level"],
            None,
        )
        if guidance_level is None:
            legacy_enabled = _nested_get(
                self._engram_user_cfg,
                ["directives", "policy", "guidance_enabled"],
                None,
            )
            if legacy_enabled is None:
                legacy_enabled = _nested_get(
                    self._engram_user_cfg,
                    ["directives", "policy", "claude_pretool_enforcement"],
                    True,
                )
            guidance_level = "warn" if bool(legacy_enabled) else "off"
        normalized_policy_level = normalize_policy_guidance_level(guidance_level)
        self._policy_level_var.set(
            _POLICY_LEVEL_VALUE_TO_DISPLAY.get(normalized_policy_level, "경고만")
        )
        self._dashboard_enabled_var.set(bool(_nested_get(cfg, ["dashboard", "enabled"], True)))
        self._external_daily_dir_var.set(
            str(
                _nested_get(
                    self._engram_user_cfg,
                    ["memory", "auto_checkpoint", "external_daily_dir"],
                    "",
                )
                or ""
            )
        )

        self._load_persona_values()

    def _load_persona_values(self):
        # Prepare and validate every source before changing a single widget.  A
        # failed load must never leave construction-time defaults actionable.
        try:
            if _USER_PERSONA_PATH.exists():
                loaded = yaml.safe_load(_USER_PERSONA_PATH.read_text(encoding="utf-8"))
                if loaded is not None and not isinstance(loaded, dict):
                    raise ValueError("persona.user.yaml must contain a mapping")
                user_persona = loaded or {}
            else:
                user_persona = {}
            db_baseline = get_persona_db_baseline()
            numeric_db: dict[str, float] = {}
            for field in _PERSONA_NUMERIC_FIELDS:
                raw = db_baseline.get(field)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0.0 <= float(raw) <= 1.0:
                    raise ValueError(f"DB baseline {field} is missing or invalid")
                numeric_db[field] = round(float(raw), 2)
                user_raw = user_persona.get(field)
                if user_raw is not None and (isinstance(user_raw, bool) or not isinstance(user_raw, (int, float)) or not 0.0 <= float(user_raw) <= 1.0):
                    raise ValueError(f"persona.user.yaml {field} is invalid")
            numeric_values = {
                field: (_coerce_persona_number(user_persona[field], _PERSONA_DEFAULTS[field]), True)
                if field in user_persona else (numeric_db[field], False)
                for field in _PERSONA_NUMERIC_FIELDS
            }
        except Exception as exc:
            self._persona_load_ok = False
            self._persona_db_baselines = {}
            for btn in self._persona_numeric_overwrite_btns.values():
                btn.state(["disabled"])
            self._persona_banner_var.set(f"페르소나 로드 실패: {exc} — DB 반영과 페르소나 파일 저장이 차단되었습니다.")
            return

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
            value, pinned = numeric_values[field]

            self._persona_numeric_vars[field].set(value)
            self._persona_numeric_pin_vars[field].set(pinned)
            self._persona_numeric_label_vars[field].set(f"{value:.2f}")

        self._persona_db_baselines = numeric_db
        self._persona_load_ok = True
        for btn in self._persona_numeric_overwrite_btns.values():
            btn.state(["!disabled"])
        self._update_persona_banner(user_persona)

    def _update_persona_banner(self, user_persona: dict | None = None):
        if user_persona is None:
            user_persona = _safe_load_yaml(_USER_PERSONA_PATH)

        if _persona_has_custom_override(user_persona):
            self._persona_banner_var.set("현재 커스텀 페르소나가 적용되어 있습니다. 원하는 스타일로 계속 조정할 수 있습니다.")
        else:
            self._persona_banner_var.set("현재 기본 페르소나가 적용되어 있습니다. 커스텀 페르소나를 적용해 보세요.")

    # ─────────────────────────────────────────────────── 파일 탐색 ──

    def _grid_values_valid(self, path: str) -> tuple[bool, str]:
        return validate_sprite_grid(
            path, self._grid_columns_var.get(), self._grid_rows_var.get(),
            self._grid_cell_width_var.get(), self._grid_cell_height_var.get(), self._grid_chroma_var.get(),
        )

    def _on_character_source_mode_changed(self, _event=None) -> None:
        self._apply_character_source_mode()
        if character_source_mode_from_display(self._char_source_mode_var.get()) == "sprite_grid":
            self._reload_manifest_editor()

    def _apply_character_source_mode(self) -> None:
        """Enable only the inputs that belong to the selected mode; retain all values."""
        mode = character_source_mode_from_display(self._char_source_mode_var.get())
        static_state = "normal" if mode == "static" else "disabled"
        sequence_state = "normal" if mode == "sequence" else "disabled"
        grid_state = "normal" if mode == "sprite_grid" else "disabled"
        self._char_path_entry.configure(state=static_state if mode == "static" else sequence_state)
        self._char_file_button.configure(state=static_state)
        self._char_dir_button.configure(state=sequence_state)
        for widget in self._grid_controls:
            widget.configure(state=grid_state)
        if hasattr(self, "_manifest_controls"):
            readonly_controls = (
                self._manifest_state_combo,
                self._manifest_selection_combo,
                self._manifest_transform_combo,
                self._manifest_vfx_combo,
            )
            for widget in self._manifest_controls:
                widget.configure(state=(
                    "readonly"
                    if grid_state == "normal" and any(widget is control for control in readonly_controls)
                    else grid_state
                ))
            if mode != "sprite_grid":
                self._manifest_status_var.set("Sprite state manifest는 sprite grid 모드에서만 편집할 수 있습니다.")
        self._update_grid_status()

    def _update_grid_status(self) -> None:
        valid, message = self._grid_values_valid(self._grid_path_var.get().strip())
        self._grid_status_var.set(("Grid: " + message) if valid else ("Grid 확인: " + message))

    def _on_grid_value_changed(self, *_args) -> None:
        self._update_grid_status()
        self._update_grid_chroma_swatch()

    def _manifest_pack_id(self) -> str:
        return str(_nested_get(self._cfg, ["overlay", "character", "reactions", "pack"], "engram") or "engram")

    def _reload_manifest_editor(self) -> None:
        if character_source_mode_from_display(self._char_source_mode_var.get()) != "sprite_grid":
            self._manifest_status_var.set("Sprite state manifest는 sprite grid 모드에서만 편집할 수 있습니다.")
            return
        pack = resolve_reaction_pack(self._manifest_pack_id())
        if pack.source == "disabled" or pack.sprite_sheet is None:
            self._manifest_status_var.set("유효한 reaction pack을 찾을 수 없습니다.")
            return
        try:
            raw = yaml.safe_load((pack.sprite_sheet.parent / "manifest.yaml").read_text(encoding="utf-8"))
            states = raw.get("states", {}) if isinstance(raw, dict) else {}
            if not isinstance(states, dict) or not states:
                raise ValueError("states가 없습니다.")
            self._manifest_raw = raw
            self._manifest_cell_count = pack.columns * pack.rows
            self._manifest_state_combo["values"] = tuple(states.keys())
            self._manifest_state_var.set(next(iter(states)))
            self._manifest_select_state()
            self._manifest_status_var.set(f"{pack.source} pack '{self._manifest_pack_id()}' — 저장하면 사용자 팩으로 복사됩니다.")
        except Exception as exc:
            self._manifest_status_var.set(f"manifest 읽기 실패: {exc}")

    def _manifest_select_state(self, _event=None) -> None:
        states = getattr(self, "_manifest_raw", {}).get("states", {})
        state = states.get(self._manifest_state_var.get(), {}) if isinstance(states, dict) else {}
        self._manifest_frames_var.set(", ".join(str(value) for value in state.get("frames", [])))
        self._manifest_selection_var.set(str(state.get("selection", "fixed")))
        self._manifest_transform_var.set(manifest_transform_to_display(state.get("transform", "none")))
        self._manifest_vfx_var.set(manifest_vfx_to_display(state.get("vfx", "none")))
        self._manifest_frame_ms_var.set(str(state.get("frame_ms", state.get("dwell_ms", 600))))
        self._manifest_dwell_ms_var.set(str(state.get("dwell_ms", "")))

    def _save_manifest_state(self) -> None:
        raw = getattr(self, "_manifest_raw", None)
        name = self._manifest_state_var.get()
        if not isinstance(raw, dict) or not name:
            self._reload_manifest_editor()
            return
        value = {
            "frames": self._manifest_frames_var.get(),
            "selection": self._manifest_selection_var.get(),
            "transform": manifest_transform_from_display(self._manifest_transform_var.get()),
            "vfx": manifest_vfx_from_display(self._manifest_vfx_var.get()),
            "frame_ms": self._manifest_frame_ms_var.get(),
            "dwell_ms": self._manifest_dwell_ms_var.get(),
        }
        valid, reason = validate_manifest_state(value, getattr(self, "_manifest_cell_count", 0))
        if not valid:
            self._manifest_status_var.set("저장하지 않음: " + reason)
            return
        states = raw.setdefault("states", {})
        old = states.get(name, {})
        states[name] = {**old, "frames": [int(part.strip()) for part in value["frames"].split(",") if part.strip()], "selection": value["selection"], "transform": value["transform"], "vfx": value["vfx"], "frame_ms": int(value["frame_ms"])}
        if value["dwell_ms"].strip():
            states[name]["dwell_ms"] = int(value["dwell_ms"])
        else:
            states[name].pop("dwell_ms", None)
        try:
            save_reaction_manifest(self._manifest_pack_id(), raw)
            self._manifest_status_var.set("사용자 reaction pack에 저장됨 — overlay가 즉시 다시 읽습니다.")
            self._reload_manifest_editor()
            if self._on_saved:
                self._on_saved()
        except Exception as exc:
            self._manifest_status_var.set(f"manifest 저장 실패: {exc}")

    def _open_manifest_editor(self) -> None:
        try:
            open_reaction_manifest(self._manifest_pack_id())
        except Exception as exc:
            self._manifest_status_var.set(f"YAML 열기 실패: {exc}")

    def _update_grid_chroma_swatch(self) -> None:
        if not hasattr(self, "_grid_chroma_swatch"):
            return
        color = self._grid_chroma_var.get().strip()
        try:
            int(color[1:], 16)
            is_color = len(color) == 7 and color.startswith("#")
        except ValueError:
            is_color = False
        try:
            if is_color:
                canonical = color.upper()
                self._grid_chroma_swatch.config(bg=canonical, activebackground=canonical, text=canonical)
            else:
                self._grid_chroma_swatch.config(bg="SystemButtonFace", activebackground="SystemButtonFace", text="색상 선택")
        except tk.TclError:
            pass

    def _pick_grid_chroma_color(self) -> None:
        current = self._grid_chroma_var.get().strip()
        try:
            _rgb, hex_color = colorchooser.askcolor(color=current, parent=self.window, title="Sprite chroma 색상 선택")
        except tk.TclError:
            _rgb, hex_color = colorchooser.askcolor(parent=self.window, title="Sprite chroma 색상 선택")
        if hex_color:
            self._grid_chroma_var.set(hex_color.upper())

    def _virtual_screen_origin(self) -> tuple[int, int]:
        """Return the virtual desktop origin used by ImageGrab on Windows."""
        try:
            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
        except Exception:
            return self.window.winfo_vrootx(), self.window.winfo_vrooty()

    def _start_grid_eyedropper(self) -> None:
        """Capture first, then use a near-transparent crosshair window for a safe sample."""
        self._cancel_grid_eyedropper()
        try:
            snapshot = ImageGrab.grab(all_screens=True)
        except Exception as exc:
            messagebox.showerror("스포이트", f"화면을 캡처할 수 없습니다.\n{exc}", parent=self.window)
            return
        origin = self._virtual_screen_origin()
        try:
            picker = tk.Toplevel(self.window)
            picker.overrideredirect(True)
            picker.attributes("-topmost", True)
            picker.attributes("-alpha", 0.01)
            picker.configure(cursor="crosshair")
            picker.geometry(f"{snapshot.width}x{snapshot.height}{origin[0]:+d}{origin[1]:+d}")
            picker.bind("<Button-1>", lambda event: self._finish_grid_eyedropper(event, snapshot, origin))
            picker.bind("<Escape>", lambda _event: self._cancel_grid_eyedropper())
            picker.focus_force()
            self._grid_eyedropper_window = picker
            self._grid_status_var.set("스포이트: 화면의 색을 클릭하세요 (Esc 취소)")
        except tk.TclError as exc:
            messagebox.showerror("스포이트", f"스포이트를 시작할 수 없습니다.\n{exc}", parent=self.window)

    def _finish_grid_eyedropper(self, event, snapshot: Image.Image, origin: tuple[int, int]) -> None:
        color = sample_snapshot_color(snapshot, event.x_root, event.y_root, origin)
        self._cancel_grid_eyedropper()
        if color:
            self._grid_chroma_var.set(color)
        else:
            self._grid_status_var.set("스포이트: 화면 범위 밖의 위치입니다.")

    def _cancel_grid_eyedropper(self) -> None:
        picker = getattr(self, "_grid_eyedropper_window", None)
        self._grid_eyedropper_window = None
        if picker is not None:
            try:
                picker.destroy()
            except tk.TclError:
                pass

    def _browse_char_file(self):
        path = filedialog.askopenfilename(
            parent=self.window,
            title="캐릭터 이미지 선택 (.png)",
            filetypes=[("PNG 이미지", "*.png"), ("모든 파일", "*.*")],
        )
        if path:
            self._char_path_var.set(path)
            self._update_grid_status()

    def _browse_grid_file(self):
        path = filedialog.askopenfilename(parent=self.window, title="Sprite grid PNG 선택", filetypes=[("PNG 이미지", "*.png")])
        if path:
            self._grid_path_var.set(path)
            self._update_grid_status()

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

    def _browse_external_daily_dir(self):
        path = filedialog.askdirectory(parent=self.window, title="Obsidian Daily Note 디렉터리 선택")
        if path:
            self._external_daily_dir_var.set(path)

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
        if not self._persona_load_ok:
            # Other settings are intentionally still saved by _do_save().
            self._persona_banner_var.set("페르소나 로드 실패 상태입니다. 페르소나 파일은 저장하지 않았습니다.")
            return 0
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
            try:
                if has_user_persona_override():
                    complete_tutorial_step("persona_setup", source="settings_save")
            except Exception:
                pass
            if self._on_saved:
                try:
                    self._on_saved()
                except Exception:
                    pass
            if self._persona_load_ok:
                self._update_persona_banner()
                self._show_toast(f"저장되었습니다. 슬라이더 고정 {pinned_count}/4, 나머지는 adaptive로 유지됩니다.")
            else:
                # _save_persona_user_file() deliberately skipped the rewrite.
                # Keep its failure banner visible instead of replacing it with a
                # normal persona success message.
                self._show_toast("일반 설정은 저장되었습니다. 페르소나는 로드 실패 상태라 저장하지 않았습니다.")
            if self._policy_sync_warnings:
                messagebox.showwarning(
                    "정책 가이드 일부 적용 실패",
                    "설정은 저장되었고 작업은 차단되지 않습니다.\n\n" + "\n".join(self._policy_sync_warnings),
                    parent=self.window,
                )
        except Exception as e:
            messagebox.showerror("저장 실패", str(e), parent=self.window)

    def _do_save(self):
        # Validate the active source before loading/mutating/saving the user YAML.  _save()
        # owns user-visible error handling, so failed validation cannot invoke callbacks/toasts.
        mode = character_source_mode_from_display(self._char_source_mode_var.get())
        provider_display = self._provider_var.get().strip()
        provider = normalize_cli_provider(_PROVIDER_DISPLAY_TO_VALUE.get(provider_display, provider_display))
        provider_cli: dict[str, str] = {}
        key = model_key(provider)
        if key: provider_cli[key] = self._provider_model_var.get().strip()
        ekey = effort_key(provider)
        if ekey: provider_cli[ekey] = self._provider_effort_var.get().strip()
        invalid = validate_cli(provider, provider_cli, self._on_get_ollama_models() if self._on_get_ollama_models else [])
        if invalid: raise ValueError(invalid)
        grid_values = (
            self._grid_path_var.get().strip(), self._grid_columns_var.get(), self._grid_rows_var.get(),
            self._grid_cell_width_var.get(), self._grid_cell_height_var.get(), self._grid_chroma_var.get().strip(),
        )
        valid, reason = validate_character_source(mode, self._char_path_var.get().strip(), grid_values)
        if not valid:
            raise ValueError(f"캐릭터 소스 설정: {reason}")

        # 기존 user.yaml을 베이스로 사용 (기존 설정 보존)
        user = _safe_load_yaml(_USER_CONFIG_PATH)

        # ── 오버레이 탭 ──
        char_path = self._char_path_var.get().strip()
        _nested_set(user, ["overlay", "character", "name"], char_path or None)
        _nested_set(user, ["overlay", "character", "source_mode"], mode)
        legacy_body_motion = bool(self._legacy_body_motion_var.get())
        _nested_set(user, ["overlay", "character", "effects", "legacy_body_motion"], True if legacy_body_motion else None)
        if mode == "sprite_grid":
            _nested_set(user, ["overlay", "character", "reactions", "grid"], {
                "columns": int(self._grid_columns_var.get()), "rows": int(self._grid_rows_var.get()),
                "cell_width": int(self._grid_cell_width_var.get()), "cell_height": int(self._grid_cell_height_var.get()),
            })
            _nested_set(user, ["overlay", "character", "reactions", "chroma_key"], self._grid_chroma_var.get().strip())
            _nested_set(user, ["overlay", "character", "reactions", "sprite_sheet"], self._grid_path_var.get().strip())

        flip_on = bool(self._flip_var.get())
        _nested_set(user, ["overlay", "flip_horizontal"], True if flip_on else None)

        height = round(self._char_height_var.get(), 3)
        # Compare with the shipped/editable base config, not the currently
        # merged setting. Otherwise moving an existing user override back to
        # the default leaves the old override in place and the live reload sees
        # no size change.
        base_cfg = _safe_load_yaml(resolve_editable_overlay_path("config/overlay.yaml"))
        default_height = _nested_get(base_cfg, ["overlay", "char_height_ratio"], 0.125)
        _nested_set(user, ["overlay", "char_height_ratio"], character_height_override_value(height, float(default_height)))

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

        provider_key = model_key(normalize_cli_provider(provider_value))
        provider_model = self._provider_model_var.get().strip()
        if provider_key:
            _nested_set(user, ["cli", provider_key], provider_model or None)
        if normalize_cli_provider(provider_value) == "claude-code" and not provider_model:
            # Explicit GUI "direct" selection must not retain legacy local routing.
            _nested_set(user, ["cli", "ollama_model"], None)
        effort = self._provider_effort_var.get().strip()
        effort_cfg_key = effort_key(normalize_cli_provider(provider_value))
        if effort_cfg_key:
            _nested_set(user, ["cli", effort_cfg_key], effort or None)

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
        _nested_set(
            engram_user,
            ["memory", "auto_checkpoint", "external_daily_dir"],
            self._external_daily_dir_var.get().strip(),
        )
        policy_level = _POLICY_LEVEL_DISPLAY_TO_VALUE.get(
            self._policy_level_var.get(),
            "warn",
        )
        policy_guidance = policy_level != "off"
        _nested_set(
            engram_user,
            ["directives", "policy", "guidance_level"],
            policy_level,
        )
        _nested_set(engram_user, ["directives", "policy", "guidance_enabled"], None)
        _nested_set(
            engram_user,
            ["directives", "policy", "claude_pretool_enforcement"],
            None,
        )
        _ENGRAM_USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ENGRAM_USER_CONFIG_PATH.write_text(
            yaml.safe_dump(engram_user, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        # runtime_config 캐시가 예전 값을 들고 있을 수 있어 방금 저장한 값으로 직접 동기화한다.
        from core.integrations.engram_bootstrap import (
            sync_claude_pretool_hook,
            sync_codex_pretool_hook,
            sync_copilot_pretool_hook,
            sync_gemini_pretool_hook,
            sync_sessionstart_hook,
        )
        from core.integrations.policy_guidance_state import sync_policy_guidance_disabled_marker

        sync_results = {
            "상태 marker": sync_policy_guidance_disabled_marker(policy_guidance),
            "SessionStart": sync_sessionstart_hook(auto_inject),
            "Claude": sync_claude_pretool_hook(policy_guidance),
            "Codex": sync_codex_pretool_hook(policy_guidance),
            "Copilot": sync_copilot_pretool_hook(policy_guidance),
            "Gemini": sync_gemini_pretool_hook(policy_guidance),
        }
        self._policy_sync_warnings = [
            f"{name}: {result.get('error') or '적용 실패'}"
            for name, result in sync_results.items()
            if not result.get("ok")
        ]
        if self._policy_sync_warnings:
            self._policy_status_var.set("일부 적용 실패 — 작업 차단 없음")
        elif policy_guidance:
            codex_status = sync_results["Codex"]
            codex_note = (
                "/hooks 승인 필요"
                if codex_status.get("trust_required")
                else "/hooks에서 승인 상태 확인"
            )
            behavior = "Agent 위반 차단 · 사람 경고" if policy_level == "enforce_agents" else "모두 경고만"
            self._policy_status_var.set(
                f"{behavior} · Claude·Copilot·Gemini 적용됨 · Codex 설정됨 — {codex_note}"
            )
        else:
            self._policy_status_var.set("정책 가이드 OFF · Git advisor backend 실행 안 함")

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
