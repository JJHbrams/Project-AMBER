"""engram CLI를 캐릭터/커서 위치 기준으로 터미널을 스폰하는 관리자.

좌표계:
- wt --pos 는 논리 픽셀(DPI-unaware screen coordinates)을 기대한다.
- SetWindowPos(물리 보정)는 Per-Monitor V2 DPI-aware context에서 호출해야 한다.
- Python DPI-unaware API 로 얻은 논리 픽셀을 물리 픽셀로 변환하거나
  물리 픽셀을 논리 픽셀로 역변환해서 각 API에 맞게 전달한다.
"""

import ctypes
import ctypes.wintypes
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process

from overlay.config import get_cli_provider, get_workdir, load_cfg, normalize_cli_provider

ENGRAM_CMD = Path.home() / ".engram" / "engram-copilot.cmd"
ENGRAM_GEMINI_CMD = Path.home() / ".engram" / "engram-gemini.cmd"
ENGRAM_CLAUDE_CMD = Path.home() / ".engram" / "engram-claude.cmd"
ENGRAM_GOOSE_CMD = Path.home() / ".engram" / "engram-goose.cmd"
CLAUDE_CODE_CMD = "claude"
GOOSE_CMD = "goose"
OLLAMA_DEFAULT_MODEL = "qwen2.5:1.5b"
OLLAMA_BASE_URL_DEFAULT = "http://localhost:11434"
_CLAUDE_MODEL_ALIASES = {
    "default",
    "best",
    "sonnet",
    "opus",
    "haiku",
    "opusplan",
    "sonnet[1m]",
    "opus[1m]",
}

_user32 = ctypes.windll.user32
_DPI_AWARE_CTX = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2

_ENGRAM_PROFILE_NAME = "engram"
_WT_SETTINGS_PATHS = [
    Path.home() / "AppData" / "Local" / "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState" / "settings.json",
    Path.home() / "AppData" / "Local" / "Microsoft" / "Windows Terminal" / "settings.json",
]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.c_uint),
    ]


def _find_wt_settings() -> Path | None:
    for p in _WT_SETTINGS_PATHS:
        if p.exists():
            return p
    return None


def _calc_font_size(phys_mon_w: int, phys_mon_h: int, dpi_scale: float, cfg: dict) -> float:
    """모니터 짧은 축(논리 px) 기준으로 폰트 크기 계산.

    WT font.size는 DPI 적용을 받으므로 물리 해상도만 쓰면 모니터 배율이
    중복 반영되거나 무시될 수 있다. 물리 px를 논리 px로 환산해 사용한다.
    """
    tc = cfg["terminal"]
    base = tc.get("base_font_size", 14)
    ref_h = tc.get("ref_screen_height", 1080)
    scale = dpi_scale if dpi_scale > 0 else 1.0
    short = min(phys_mon_w, phys_mon_h) / scale
    return round(base * short / ref_h, 1)


def _ensure_engram_profile(font_size: float) -> bool:
    """WT settings.json에 engram 프로필이 있으면 font.size 업데이트, 없으면 생성.

    Returns True if profile exists/created.
    """
    settings_path = _find_wt_settings()
    if not settings_path:
        return False
    try:
        raw = settings_path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except Exception:
        return False

    profiles = data.setdefault("profiles", {}).setdefault("list", [])
    engram = None
    for p in profiles:
        if p.get("name") == _ENGRAM_PROFILE_NAME:
            engram = p
            break

    if engram is None:
        engram = {
            "name": _ENGRAM_PROFILE_NAME,
            "guid": "{" + str(uuid.uuid5(uuid.NAMESPACE_DNS, "engram.overlay")) + "}",
            "hidden": True,
            "commandline": "cmd.exe",
        }
        profiles.append(engram)

    engram.setdefault("font", {})["size"] = font_size

    try:
        settings_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def _phys_monitor_work(hmon_pyhandle) -> tuple:
    """DPI-aware GetMonitorInfo 로 물리 픽셀 work rect 반환."""
    hmon = ctypes.wintypes.HANDLE(int(hmon_pyhandle))
    old = _user32.GetThreadDpiAwarenessContext()
    _user32.SetThreadDpiAwarenessContext(_DPI_AWARE_CTX)
    try:
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)
    finally:
        _user32.SetThreadDpiAwarenessContext(old)


def _get_monitor_info(probe_lx: int, probe_ly: int):
    """논리 probe 좌표로 모니터를 찾아 (log_work, phys_work, dpi_scale) 반환."""
    hmon = win32api.MonitorFromPoint((probe_lx, probe_ly), 2)
    log_work = win32api.GetMonitorInfo(hmon)["Work"]
    phys_work = _phys_monitor_work(hmon)
    ll, lt, lr, lb = log_work
    pl, pt, pr, pb = phys_work
    log_w = (lr - ll) or 1
    phys_w = pr - pl
    return log_work, phys_work, phys_w / log_w


def _to_phys(lx: int, ly: int, log_work: tuple, phys_work: tuple) -> tuple:
    """논리 점을 물리 점으로 변환."""
    ll, lt, lr, lb = log_work
    pl, pt, pr, pb = phys_work
    log_w, log_h = (lr - ll) or 1, (lb - lt) or 1
    phys_w, phys_h = pr - pl, pb - pt
    return (pl + round((lx - ll) * phys_w / log_w), pt + round((ly - lt) * phys_h / log_h))


def _to_log(px: int, py: int, log_work: tuple, phys_work: tuple) -> tuple:
    """물리 점을 논리 점으로 변환 (wt --pos 용)."""
    ll, lt, lr, lb = log_work
    pl, pt, pr, pb = phys_work
    log_w, log_h = (lr - ll) or 1, (lb - lt) or 1
    phys_w, phys_h = (pr - pl) or 1, (pb - pt) or 1
    return (ll + round((px - pl) * log_w / phys_w), lt + round((py - pt) * log_h / phys_h))


def _calc_term_geometry(anchor_px, anchor_py, char_px, char_w_phys, pl, pt, pr, pb, dpi_scale, cfg):
    """물리 픽셀 기준 터미널 위치/크기 반환 (x, y, w, h, cols, rows)."""
    tc = cfg["terminal"]
    phys_mon_w, phys_mon_h = pr - pl, pb - pt

    # portrait 모니터면 width/height ratio 스왑
    landscape = phys_mon_w >= phys_mon_h
    w_ratio = tc["width_ratio"] if landscape else tc["height_ratio"]
    h_ratio = tc["height_ratio"] if landscape else tc["width_ratio"]
    win_w = int(phys_mon_w * w_ratio)
    win_h = int(phys_mon_h * h_ratio)

    mon_center = (pl + pr) // 2
    char_center = char_px + char_w_phys // 2

    # 캐릭터 중심이 모니터 오른쪽 반에 있거나, 터미널이 화면 밖으로 나가면 왼쪽으로 플립
    if anchor_px + win_w <= pr and char_center <= mon_center:
        term_x = anchor_px
    else:
        # 터미널 오른쪽 끝 = 캐릭터 왼쪽 끝 (topmost 캐릭터에 가려지지 않도록)
        term_x = char_px - win_w

    term_x = max(pl, min(term_x, pr - win_w))

    # anchor_py = 터미널 BOTTOM 기준. 항상 위로 올라감
    term_y = anchor_py - win_h
    term_y = max(pt, min(term_y, pb - win_h))

    # cols/rows: 물리 px / 물리 char 크기 (dpi_scale이 분자·분모에서 상쇄됨)
    cw = max(1, round(tc["base_char_w"] * dpi_scale))
    ch = max(1, round(tc["base_char_h"] * dpi_scale))
    cols = max(tc["min_cols"], win_w // cw)
    rows = max(tc["min_rows"], win_h // ch)

    return term_x, term_y, win_w, win_h, cols, rows


_WT_WINDOW_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"


def _find_wt_hwnd_by_class() -> list[int]:
    """CASCADIA 클래스 창 모두 반환."""
    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetClassName(hwnd) == _WT_WINDOW_CLASS:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def _move_window_phys(hwnd: int, x: int, y: int, w: int, h: int):
    """물리 픽셀 기준으로 창 이동 + 크기 조정 + 포커스."""
    old = _user32.GetThreadDpiAwarenessContext()
    _user32.SetThreadDpiAwarenessContext(_DPI_AWARE_CTX)
    try:
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOP,
            x,
            y,
            w,
            h,
            win32con.SWP_SHOWWINDOW,
        )
    finally:
        _user32.SetThreadDpiAwarenessContext(old)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _extract_executable(command: str) -> str:
    if not command.strip():
        return ""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def _inject_persona_hint(launch_args: list[str]) -> list[str]:
    """cmd /k 실행 시 시작 줄에 persona 설정 힌트를 출력한다."""
    if len(launch_args) < 3:
        return launch_args
    if launch_args[0].lower() != "cmd" or launch_args[1].lower() != "/k":
        return launch_args

    base = subprocess.list2cmdline(launch_args[2:])
    hint = "echo [engram] Hint: %USERPROFILE%\\.engram\\persona.user.yaml values are pinned and override adaptive persona"
    return ["cmd", "/k", f"{hint} & echo. & {base}"]


def _force_kill_process_tree(pid: int | None):
    """직접 스폰한 콘솔 프로세스 트리를 강제 종료한다.

    주의: Windows Terminal(wt) 경로에는 적용하지 않는다.
    """
    if not pid or pid <= 0 or pid == os.getpid():
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            check=False,
        )
    except Exception:
        pass


def _normalize_model_id(model_id: str) -> str:
    value = str(model_id or "").strip().lower()
    if value.endswith("[1m]"):
        return value[:-4]
    return value


def _looks_like_claude_model(model_id: str) -> bool:
    value = _normalize_model_id(model_id)
    if not value:
        return False
    if value in _CLAUDE_MODEL_ALIASES:
        return True
    return value.startswith("claude-")


def _query_ollama_capabilities(ollama_command: str, model_id: str) -> set[str]:
    """ollama show 출력에서 capabilities 목록을 파싱한다."""
    cmd = str(ollama_command or "").strip() or "ollama"
    model = str(model_id or "").strip()
    if not model:
        return set()

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [cmd, "show", model],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=flags,
            check=False,
        )
    except Exception:
        return set()

    if result.returncode != 0:
        return set()

    caps: set[str] = set()
    in_caps = False
    for raw in result.stdout.splitlines():
        line = raw.rstrip()
        stripped = line.strip().lower()
        if not stripped:
            continue
        if stripped == "capabilities":
            in_caps = True
            continue
        if in_caps and line.startswith("  ") and not line.startswith("    "):
            break
        if in_caps and line.startswith("    "):
            cap = stripped.split()[0]
            if cap:
                caps.add(cap)
    return caps


def _resolve_ollama_launch(cli_cfg: dict, requested_model: str | None = None) -> tuple[str, list[str], str]:
    ollama_command = str(cli_cfg.get("ollama_command") or "ollama").strip() or "ollama"
    selected_model = str(requested_model or cli_cfg.get("ollama_model") or OLLAMA_DEFAULT_MODEL).strip() or OLLAMA_DEFAULT_MODEL

    # Goose(MCP agent)가 설치되어 있으면 MCP 연동 shim을 우선 사용한다.
    if ENGRAM_GOOSE_CMD.exists():
        return (
            "ollama",
            ["cmd", "/k", str(ENGRAM_GOOSE_CMD)],
            f"engram-goose.cmd (Ollama+MCP, {selected_model})",
            {"GOOSE_MODEL": selected_model},
        )
    if shutil.which(GOOSE_CMD):
        return (
            "ollama",
            ["cmd", "/k", GOOSE_CMD, "session"],
            f"goose session ({selected_model})",
            {"GOOSE_MODEL": selected_model, "GOOSE_PROVIDER": "ollama"},
        )

    # fallback: 순수 ollama run (MCP 없음)
    return "ollama", ["cmd", "/k", ollama_command, "run", selected_model], f"{ollama_command} run {selected_model}", {}


def _resolve_provider_launch(cfg: dict, provider: str) -> tuple[str, list[str], str, dict[str, str], list[str]]:
    """선택된 provider를 실행할 launch 인자와 표시용 라벨을 반환한다."""
    normalized = normalize_cli_provider(provider)
    cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
    if not isinstance(cli_cfg, dict):
        cli_cfg = {}
    ollama_model = str(cli_cfg.get("ollama_model") or "").strip()
    env_overrides: dict[str, str] = {}
    warnings: list[str] = []

    if normalized in {"claude-code", "claude-code-ollama"}:
        claude_args: list[str] = []
        label = "claude"
        selected_model = ollama_model
        if normalized != "claude-code-ollama":
            selected_model = str(cli_cfg.get("claude_model") or ollama_model).strip()
        elif not selected_model:
            selected_model = OLLAMA_DEFAULT_MODEL

        if selected_model:
            # Claude alias/id가 아닌 모델명은 local Ollama 모델로 간주해 base URL을 주입한다.
            if not _looks_like_claude_model(selected_model):
                if not os.environ.get("ANTHROPIC_BASE_URL"):
                    ollama_base_url = str(cli_cfg.get("ollama_base_url") or OLLAMA_BASE_URL_DEFAULT).strip() or OLLAMA_BASE_URL_DEFAULT
                    env_overrides["ANTHROPIC_BASE_URL"] = ollama_base_url

                ollama_command = str(cli_cfg.get("ollama_command") or "ollama").strip() or "ollama"
                capabilities = _query_ollama_capabilities(ollama_command, selected_model)
                fallback_mode = str(cli_cfg.get("claude_ollama_no_tools_fallback") or "ollama").strip().lower()
                if capabilities and "tools" not in capabilities and fallback_mode == "ollama":
                    warnings.append(f"Ollama model '{selected_model}' has no tools capability; falling back to provider=ollama for this launch.")
                    resolved_provider, launch_args, resolved_label, extra_env = _resolve_ollama_launch(cli_cfg, requested_model=selected_model)
                    return resolved_provider, launch_args, resolved_label, extra_env, warnings

            claude_args = ["--model", selected_model]
            label = f"claude --model {selected_model}"
        if ENGRAM_CLAUDE_CMD.exists():
            return normalized, ["cmd", "/k", str(ENGRAM_CLAUDE_CMD), *claude_args], ENGRAM_CLAUDE_CMD.name, env_overrides, warnings
        return normalized, ["cmd", "/k", CLAUDE_CODE_CMD, *claude_args], label, env_overrides, warnings

    if normalized == "ollama":
        resolved_provider, launch_args, label, extra_env = _resolve_ollama_launch(cli_cfg, requested_model=ollama_model or None)
        env_overrides.update(extra_env)
        return resolved_provider, launch_args, label, env_overrides, warnings

    if normalized == "gemini":
        if ENGRAM_GEMINI_CMD.exists():
            return normalized, ["cmd", "/k", str(ENGRAM_GEMINI_CMD)], ENGRAM_GEMINI_CMD.name, env_overrides, warnings
        gemini_command = str(cli_cfg.get("gemini_command") or "gemini").strip() or "gemini"
        label = "gemini"
        return normalized, ["cmd", "/k", gemini_command], label, env_overrides, warnings

    return "copilot", ["cmd", "/k", str(ENGRAM_CMD)], "engram-copilot.cmd", env_overrides, warnings


class ChatTerminal:
    def __init__(self, provider: str | None = None):
        self._proc = None
        self._hwnd = None
        self._provider = normalize_cli_provider(provider)
        self._spawn_mode = "wt"

    def set_provider(self, provider: str):
        normalized = normalize_cli_provider(provider)
        if normalized == self._provider:
            return
        self.kill()
        self._provider = normalized

    def get_provider(self) -> str:
        return self._provider

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _get_hwnd(self) -> int | None:
        """내가 스폰한 wt 창 HWND 반환. 죽었으면 None."""
        if self._hwnd:
            # 아직 살아있는지 확인
            if win32gui.IsWindow(self._hwnd):
                return self._hwnd
            self._hwnd = None
        return None

    def _find_new_hwnd(self, before: set[int]) -> int | None:
        """스폰 전후 HWND diff로 새 창 탐색. 최대 5초 대기."""
        import time

        for _ in range(50):
            time.sleep(0.1)
            after = set(_find_wt_hwnd_by_class())
            new = after - before
            if new:
                return new.pop()
        return None

    def show_at_overlay(self, char_x: int, char_y: int, char_w: int, char_h: int):
        """오버레이 클릭 시: 논리 좌표 -> 물리 좌표 변환 후 wt 스폰 또는 이동."""
        cfg = load_cfg()
        tc = cfg["terminal"]

        log_work, phys_work, dpi_scale = _get_monitor_info(char_x + char_w // 2, char_y + char_h // 2)
        pl, pt, pr, pb = phys_work

        # 논리 앵커 -> 물리 앵커
        # anchor = 터미널 BOTTOM edge 위치
        # char_y - offset = 캐릭터 top에서 위로 (터미널이 캐릭터 위에 뜸)
        anchor_lx = char_x + int(char_w * tc["anchor_x_ratio"])
        anchor_ly = char_y + int(char_h * tc["anchor_y_ratio"])
        anchor_px, anchor_py = _to_phys(anchor_lx, anchor_ly, log_work, phys_work)

        char_px, _ = _to_phys(char_x, char_y, log_work, phys_work)
        char_w_phys = round(char_w * dpi_scale)

        x, y, win_w, win_h, cols, rows = _calc_term_geometry(anchor_px, anchor_py, char_px, char_w_phys, pl, pt, pr, pb, dpi_scale, cfg)

        # 모니터 해상도 기반 폰트 크기 조절
        phys_mon_w, phys_mon_h = pr - pl, pb - pt
        font_size = _calc_font_size(phys_mon_w, phys_mon_h, dpi_scale, cfg)
        _ensure_engram_profile(font_size)

        import logging

        _log_dir = Path.home() / ".engram"
        _log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(_log_dir / "overlay_debug.log"), level=logging.DEBUG)
        logging.debug(
            f"char=({char_x},{char_y},{char_w},{char_h}) "
            f"anchor_l=({anchor_lx},{anchor_ly}) anchor_p=({anchor_px},{anchor_py}) "
            f"char_center={char_px + round(char_w_phys/2)} mon_center={(pl+pr)//2} "
            f"term=({x},{y}) win=({win_w},{win_h}) "
            f"phys_work=({pl},{pt},{pr},{pb}) dpi={dpi_scale:.2f} "
            f"font_size={font_size} hwnd={self._get_hwnd()}"
        )

        hwnd = self._get_hwnd()
        if hwnd:
            _move_window_phys(hwnd, x, y, win_w, win_h)
        else:
            # wt --pos 는 논리 픽셀을 기대 → 물리→논리 변환
            log_x, log_y = _to_log(x, y, log_work, phys_work)
            self._spawn(log_x, log_y, cols, rows, cfg)
            # wt --size는 문자 단위라 실제 픽셀 크기가 다를 수 있음 → 강제 보정
            if self._hwnd:
                _move_window_phys(self._hwnd, x, y, win_w, win_h)

    def show_at_cursor(self):
        """단축키: 커서 물리 픽셀 위치 기준 스폰."""
        cfg = load_cfg()

        # DPI-aware 커서 위치
        old = _user32.GetThreadDpiAwarenessContext()
        _user32.SetThreadDpiAwarenessContext(_DPI_AWARE_CTX)
        try:
            pt_cur = ctypes.wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt_cur))
            cx, cy = pt_cur.x, pt_cur.y
        finally:
            _user32.SetThreadDpiAwarenessContext(old)

        # 커서 논리 좌표로 모니터 탐색
        log_cx, log_cy = win32api.GetCursorPos()
        log_work, phys_work, dpi_scale = _get_monitor_info(log_cx, log_cy)
        pl, pt, pr, pb = phys_work
        phys_mon_w, phys_mon_h = pr - pl, pb - pt

        tc = cfg["terminal"]
        win_w = int(phys_mon_w * tc["width_ratio"])
        win_h = int(phys_mon_h * tc["height_ratio"])

        # 모니터별 DPI 스케일을 반영해 WT 프로필 폰트를 갱신한다.
        font_size = _calc_font_size(phys_mon_w, phys_mon_h, dpi_scale, cfg)
        _ensure_engram_profile(font_size)

        cw = max(1, round(tc["base_char_w"] * dpi_scale))
        ch = max(1, round(tc["base_char_h"] * dpi_scale))
        cols = max(tc["min_cols"], win_w // cw)
        rows = max(tc["min_rows"], win_h // ch)
        term_x = max(pl, min(cx, pr - win_w))
        term_y = max(pt, min(cy, pb - win_h))
        # wt --pos 는 논리 픽셀 기대 → 물리→논리 변환 후 spawn
        log_x, log_y = _to_log(term_x, term_y, log_work, phys_work)
        self._spawn(log_x, log_y, cols, rows, cfg)
        # 정확한 물리 픽셀 위치로 보정
        if self._hwnd:
            _move_window_phys(self._hwnd, term_x, term_y, win_w, win_h)

    def _spawn(self, x: int, y: int, cols: int, rows: int, cfg: dict):
        before = set(_find_wt_hwnd_by_class())
        workdir = str(get_workdir(cfg))
        provider = normalize_cli_provider(self._provider or get_cli_provider(cfg))
        provider, launch_args, _provider_label, launch_env, launch_warnings = _resolve_provider_launch(cfg, provider)
        launch_args = _inject_persona_hint(launch_args)
        # 모든 REPL 세션을 overlay scope로 통일 (STM 프로모터가 관찰 가능하도록)
        spawn_env = {
            **os.environ,
            "ENGRAM_SCOPE_KEY": "overlay",
            "ENGRAM_CLI_PROVIDER": provider,
        }
        spawn_env.update(launch_env)

        if launch_warnings:
            import logging

            logger = logging.getLogger(__name__)
            for warning in launch_warnings:
                logger.warning("[overlay] %s", warning)

        executable = _extract_executable(launch_args[2] if len(launch_args) >= 3 else "")
        if provider in {"gemini", "claude-code", "ollama"} and executable and not shutil.which(executable):
            import logging

            logging.getLogger(__name__).warning("[overlay] %s CLI를 찾지 못했습니다: %s", provider, executable)

        try:
            cmd = [
                "wt",
                "--window",
                "new",
                "--pos",
                f"{x},{y}",
                "--size",
                f"{cols},{rows}",
            ]
            if _find_wt_settings():
                cmd += ["-p", _ENGRAM_PROFILE_NAME]
            cmd += ["--title", f"engram [{_provider_label}]"]
            cmd += ["-d", workdir, *launch_args]
            self._proc = subprocess.Popen(cmd, env=spawn_env)
            self._spawn_mode = "wt"
        except FileNotFoundError:
            self._proc = subprocess.Popen(
                launch_args,
                env=spawn_env,
                cwd=workdir,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._spawn_mode = "direct"
            return
        self._hwnd = self._find_new_hwnd(before)

    def kill(self):
        """오버레이 종료 시 터미널도 함께 종료."""
        hwnd = self._get_hwnd()
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                deadline = time.time() + 1.5
                while time.time() < deadline and win32gui.IsWindow(hwnd):
                    time.sleep(0.05)
                if win32gui.IsWindow(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_CLOSE, 0)
            except Exception:
                pass
        if self._alive():
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

        # wt 경로는 WM_CLOSE로 정리하고, direct 콘솔만 프로세스 트리 강제 종료.
        if self._spawn_mode == "direct" and self._proc is not None:
            _force_kill_process_tree(getattr(self._proc, "pid", None))

        self._proc = None
        self._hwnd = None
