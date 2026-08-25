"""오버레이 진입점 — 트레이 아이콘 + 전역 단축키 (Alt+F12) + 캐릭터 창."""

import ctypes
import json
import logging
import os
import queue
import socket
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path

import keyboard
import pystray
from PIL import Image

from .character import CharacterOverlay
from .event_api import OverlayEventPublisher
from .chat_window import ChatTerminal
from .config import (
    get_bubble_cfg,
    get_bubble_session_id,
    get_chat_mode,
    get_cli_provider,
    get_cli_model,
    get_flip_horizontal,
    get_ollama_model,
    get_permission_level,
    get_workdir,
    load_cfg,
    resolve_path,
    set_bubble_session_id,
    set_cli_provider,
    set_cli_model,
    set_ollama_model,
)
from .settings_window import open_settings
from .cli_capabilities import models as provider_models
from .remote_tunnel import TunnelManager
from .stm_server import STMServer
from .bubble.bubble_manager import BubbleManager
from .bubble.history_panel import HistoryPanel
from .bubble.initiative import InitiativeEngine, default_sources, make_persona_phraser
from .bubble.input_bar import InputBar
from .bubble.session import BubbleSessionManager
from .bubble.stm_bridge import StmBridge
from core.integrations.engram_bootstrap import (
    bubble_bootstrap_prompt,
    is_auto_inject_enabled,
    is_policy_guidance_enabled,
    sync_claude_pretool_hook,
    sync_codex_pretool_hook,
    sync_copilot_pretool_hook,
    sync_gemini_pretool_hook,
    sync_sessionstart_hook,
)
from core.integrations.policy_guidance_state import sync_policy_guidance_disabled_marker
from core.integrations.remote_provision import refresh_host_on_tunnel_up

# Claude 모델 alias — 이 외 이름은 Ollama 로컬 모델로 간주
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


def _is_ollama_routing_model(model: str) -> bool:
    m = model.lower().strip()
    return bool(m) and m not in _CLAUDE_MODEL_ALIASES and not m.startswith("claude-")


# ── Ollama 모델 캐시 (백그라운드 로드) ──────────────────────────
_ollama_model_cache: list[str] = []
_ollama_cache_lock = threading.Lock()
_ollama_cache_ready = False


def _load_ollama_models() -> None:
    global _ollama_model_cache, _ollama_cache_ready
    try:
        cfg = load_cfg()
        cli_cfg = cfg.get("cli", {}) if isinstance(cfg, dict) else {}
        base_url = str(cli_cfg.get("ollama_base_url") or "http://localhost:11434").rstrip("/")
        req = urllib.request.Request(f"{base_url}/api/tags")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        with _ollama_cache_lock:
            _ollama_model_cache = models
            _ollama_cache_ready = True
    except Exception:
        with _ollama_cache_lock:
            _ollama_cache_ready = True


def _get_ollama_model_list_snapshot() -> list[str]:
    with _ollama_cache_lock:
        return list(_ollama_model_cache)


def _reload_ollama_models() -> None:
    """캐시를 초기화하고 Ollama 모델 목록을 백그라운드에서 다시 로드한다."""
    global _ollama_cache_ready
    with _ollama_cache_lock:
        _ollama_cache_ready = False
    threading.Thread(target=_load_ollama_models, daemon=True).start()


log = logging.getLogger(__name__)

# 지속 MCP HTTP(SSE) 서버 포트 기본값
MCP_HTTP_PORT = 17385


def _get_project_root() -> Path:
    """빌드 방식에 무관하게 프로젝트 루트를 반환한다.

    - frozen(onedir): dist/engram-overlay/engram-overlay.exe → 세 단계 위
    - 개발 모드: overlay/main.py → 두 단계 위
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.parent.parent
    return Path(__file__).parent.parent


PROJECT_ROOT = _get_project_root()


def _find_mcp_python() -> str | None:
    """MCP HTTP 서버 subprocess용 Python 경로를 반환한다."""
    # 개발 모드: sys.executable이 Python이면 직접 사용
    if not getattr(sys, "frozen", False):
        return sys.executable
    # 동결(PyInstaller) 모드: overlay.user.yaml mcp.python_exe 또는 conda 기본 경로
    try:
        cfg = load_cfg()
        py = (cfg.get("mcp") or {}).get("python_exe", "")
        if py and Path(py).exists():
            return py
    except Exception:
        pass
    default = Path.home() / "miniconda3" / "envs" / "intel_engram" / "python.exe"
    if default.exists():
        return str(default)
    return None


def _find_mcp_script() -> Path | None:
    """mcp_server.py 경로를 반환한다."""
    p = (PROJECT_ROOT / "mcp_server.py").resolve()
    return p if p.exists() else None


# 프로세스 설명 이름 설정 (작업 관리자에서 구별용)
ctypes.windll.kernel32.SetConsoleTitleW("engram-overlay")
try:
    ctypes.windll.kernel32.SetFileDescriptionW("engram-overlay")
except Exception:
    pass

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("engram.overlay")
except Exception:
    pass


def _resolve_icon_path() -> Path:
    user_overlay = Path.home() / ".engram" / "overlay.png"
    for rel in ("resource/icon.png",):
        p = resolve_path(rel)
        if p.exists():
            return p
    if user_overlay.exists():
        return user_overlay
    for rel in ("resource/overlay.png",):
        p = resolve_path(rel)
        if p.exists():
            return p
    return resolve_path("resource/overlay.png")


def _make_tray_icon(app: "OverlayApp"):
    icon_path = _resolve_icon_path()
    img = Image.open(icon_path).convert("RGBA").resize((64, 64))

    def _select_provider_action(provider: str, model: str | None):
        """Bind model outside pystray's (icon, item) action arguments."""
        def action(_icon, _item=None):
            app._set_provider_model(provider, model)
        return action

    def _build_claude_items():
        """Claude Code 서브메뉴 — 직접 vs Ollama 라우팅 선택."""
        items: list = [
            pystray.MenuItem(
                "claude (직접)",
                _select_provider_action("claude-code", ""),
                checked=lambda _: app.get_cli_provider() == "claude-code",
                radio=True,
            ),
        ]
        cfg = load_cfg()
        cli = cfg.get("cli", {}) if isinstance(cfg.get("cli"), dict) else {}
        for alias in provider_models("claude-code", cli, _get_ollama_model_list_snapshot()):
            items.append(
                pystray.MenuItem(
                    f"claude: {alias}",
                    _select_provider_action("claude-code", alias),
                    checked=lambda _, mod=alias: app.get_cli_provider() == "claude-code" and app.get_cli_model("claude-code") == mod,
                    radio=True,
                )
            )
        items.append(pystray.Menu.SEPARATOR)
        with _ollama_cache_lock:
            ready = _ollama_cache_ready
            models = list(_ollama_model_cache)
        if not ready:
            items.append(pystray.MenuItem("ollama 모델 로딩 중...", None, enabled=False))
        elif models:
            for m in models:
                model = m  # 클로저 캡처
                items.append(
                    pystray.MenuItem(
                        f"ollama: {model}",
                        _select_provider_action("claude-code-ollama", model),
                        checked=lambda _, mod=model: app.get_cli_provider() == "claude-code-ollama" and app._ollama_model == mod,
                        radio=True,
                    )
                )
        else:
            items.append(pystray.MenuItem("(설치된 Ollama 모델 없음)", None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Ollama 새로고침", lambda: _reload_ollama_models()))
        return items

    def _build_ollama_items():
        """Ollama 서브메뉴 — 설치된 모델 목록."""
        with _ollama_cache_lock:
            ready = _ollama_cache_ready
            models = list(_ollama_model_cache)
        items: list = []
        if not ready:
            items.append(pystray.MenuItem("모델 로딩 중...", None, enabled=False))
        elif not models:
            items.append(pystray.MenuItem("(설치된 모델 없음)", None, enabled=False))
        else:
            items += [
                pystray.MenuItem(
                    m,
                    _select_provider_action("ollama", m),
                    checked=lambda _, mod=m: app.get_cli_provider() == "ollama" and app._ollama_model == mod,
                    radio=True,
                )
                for m in models
            ]
        items += [pystray.Menu.SEPARATOR, pystray.MenuItem("새로고침", lambda: _reload_ollama_models())]
        return items

    def _build_provider_items(provider: str, title: str):
        cfg = load_cfg(); cli = cfg.get("cli", {}) if isinstance(cfg.get("cli"), dict) else {}
        choices = provider_models(provider, cli, _get_ollama_model_list_snapshot())
        if not choices:
            return [pystray.MenuItem(title, _select_provider_action(provider, None), checked=lambda _: app.get_cli_provider() == provider, radio=True)]
        return [pystray.MenuItem(m, _select_provider_action(provider, m), checked=lambda _, mod=m: app.get_cli_provider() == provider and app.get_cli_model(provider) == mod, radio=True) for m in choices]

    menu = pystray.Menu(
        pystray.MenuItem("채팅 열기/닫기", lambda: app.toggle_chat()),
        pystray.MenuItem("대화 기록 보기", lambda: app.root.after(0, app.show_bubble_history)),
        pystray.MenuItem(
            "CLI 공급자",
            pystray.Menu(
                pystray.MenuItem(
                    "Copilot CLI",
                    pystray.Menu(lambda: _build_provider_items("copilot", "Copilot CLI")),
                    checked=lambda _: app.get_cli_provider() == "copilot",
                ),
                pystray.MenuItem(
                    "Gemini CLI",
                    pystray.Menu(lambda: _build_provider_items("gemini", "Gemini CLI")),
                    checked=lambda _: app.get_cli_provider() == "gemini",
                ),
                pystray.MenuItem(
                    "Codex CLI",
                    pystray.Menu(lambda: _build_provider_items("codex", "Codex CLI")),
                    checked=lambda _: app.get_cli_provider() == "codex",
                ),
                pystray.MenuItem(
                    "Claude Code",
                    pystray.Menu(_build_claude_items),
                    checked=lambda _: app.get_cli_provider() in {"claude-code", "claude-code-ollama"},
                ),
                pystray.MenuItem(
                    "Ollama",
                    pystray.Menu(_build_ollama_items),
                    checked=lambda _: app.get_cli_provider() == "ollama",
                ),
            ),
        ),
        pystray.MenuItem("설정", lambda: app.root.after(0, app.open_settings)),
        pystray.MenuItem("종료", lambda: app.request_quit()),
    )
    icon = pystray.Icon("engram", img, "engram overlay", menu)
    icon.title = f"engram overlay ({app.get_cli_provider()})"
    return icon


def _try_start_discord_bot():
    """DISCORD_BOT_TOKEN이 있으면 Discord 봇 시작. 패키지 없으면 조용히 스킵."""
    try:
        from discord_bot.bot import EngramDiscordBot

        bot = EngramDiscordBot()
        bot.start()
        return bot
    except ImportError as e:
        log.warning("[discord] discord.py 미설치 — 봇 비활성화: %s", e, exc_info=True)
        return None
    except Exception as e:
        log.warning("[discord] 봇 시작 실패: %s", e, exc_info=True)
        return None


class OverlayApp:
    def __init__(self):
        cfg = load_cfg()
        hotkey = cfg["overlay"]["hotkey"]
        self._cli_provider = get_cli_provider(cfg)
        self._ollama_model = get_ollama_model(cfg)
        self._chat_mode = get_chat_mode(cfg)
        self._bubble_session: "BubbleSessionManager | None" = None
        self._quitting = False
        self._quit_reason = "unknown"
        self._mcp_recovery_lock = threading.Lock()
        self._last_mcp_recovery_at = 0.0
        # 원격 SSH 리버스 터널. 실제 연결은 _deferred_startup 에서 MCP 준비 후 시작한다.
        self._tunnels = TunnelManager(
            get_port=lambda: int((load_cfg().get("mcp") or {}).get("remote_port", 17386) or 17386),
            get_auto_reconnect=lambda: bool(
                (load_cfg().get("mcp") or {}).get("tunnel_auto_reconnect", False)
            ),
            # 터널이 붙으면 원격의 skill/SessionStart hook 을 최신으로 맞춘다.
            # 이미 setup-remote.ps1 로 한 번 등록된 호스트만 대상이고, 배치 내용이
            # 바뀌지 않았으면 ssh 를 띄우지도 않는다.
            on_tunnel_up=refresh_host_on_tunnel_up,
        )
        # Ollama 모델 목록 백그라운드 로드
        threading.Thread(target=_load_ollama_models, daemon=True).start()

        self.root = tk.Tk()
        self.root.report_callback_exception = self._report_callback_exception
        self._app_icon = None
        try:
            icon_path = _resolve_icon_path()
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._app_icon)
        except Exception as e:
            log.warning("[overlay] app icon load failed: %s", e)
        self.root.withdraw()
        self._renderer_inbound: queue.Queue[dict] = queue.Queue()
        self._replace_startup_geometry_pending = False

        self.chat = ChatTerminal(provider=self._cli_provider)
        self.character = CharacterOverlay(
            self.root,
            on_activate=self.toggle_chat,
            on_set_provider=self.set_cli_provider,
            on_get_provider=self.get_cli_provider,
            on_quit=self.quit,
            on_set_provider_model=self._set_provider_model,
            on_get_ollama_models=_get_ollama_model_list_snapshot,
            on_get_ollama_model=lambda: self._ollama_model,
            on_reload_ollama_models=_reload_ollama_models,
            on_settings=self.open_settings,
            on_restart=self.restart,
            on_history=self.show_bubble_history,
            on_pointer_event=self._on_bundled_pointer_event,
        )
        # Observer geometry is an in-memory click anchor only.  Replacement
        # geometry remains owned by CharacterOverlay and is persisted there.
        self._observer_rect: tuple[int, int, int, int] | None = None
        self._bubble_anchor = "bundled"
        self._overlay_events = OverlayEventPublisher(
            cfg,
            on_failure=lambda: self._renderer_inbound.put({"type": "_renderer.failure"}),
            on_message=self._renderer_inbound.put,
        )
        if self._overlay_events.start() and self._overlay_events.mode == "replace":
            # The first renderer geometry describes its bootstrap window, not a
            # user move.  The restored Engram position wins and is acknowledged
            # back through overlay.set_position.
            self._replace_startup_geometry_pending = True
            self.character.hide_for_external_renderer()
            rect = self.character.get_phys_rect()
            self._overlay_events.publish("overlay.set_position", "idle", {"x": rect[0], "y": rect[1]})
        self.root.after(50, self._drain_external_renderer_messages)

        # 말풍선 모드 UI(렌더러/입력창/히스토리) — 가벼워서 미리 만들어둔다.
        # 실제 Claude 세션(BubbleSessionManager)은 캐릭터를 처음 클릭할 때 지연 기동한다.
        bubble_cfg = get_bubble_cfg(cfg)
        terminal_cfg = cfg.get("terminal") or {}
        self._bubble_manager = BubbleManager(self.root, self._get_bubble_anchor_rect, bubble_cfg, terminal_cfg)
        self._bubble_input = InputBar(
            self.root,
            self._get_bubble_anchor_rect,
            bubble_cfg,
            terminal_cfg,
            get_speech_rect=self._bubble_manager.get_speech_rect,
        )
        self._bubble_history = HistoryPanel(
            self.root, get_stm_port=lambda: self._stm_server.port, scope_key="overlay",
            cfg_bubble=bubble_cfg, get_anchor_rect=self._get_bubble_anchor_rect,
        )

        # 능동적 상주(initiative) — 유휴 시 캐릭터가 스스로 말을 건다.
        # 활동 추적: 마지막 상호작용 시각 + 현재 턴 진행 여부(발화 가능 판정용).
        self._bubble_last_activity = time.monotonic()
        self._bubble_turn_active = False
        # 자율발화를 클릭해 입력창을 연 상태 — 여기서 제출하면 engaged, 그냥 닫으면
        # acknowledged_no_reply 로 갈린다. 클릭 시점엔 아직 결과를 모른다.
        self._nudge_awaiting_reply = False
        self._nudge_engage_live = False  # 살아있는 발화에 답하는 중인가(vs 지난 발화 되살림)
        self._pending_nudge_text = ""   # 답장 첫 마디에 얹을 직전 발화 문구
        self._ignored_streaks: dict[str, int] = {}  # 소스별 연속 무시 횟수(세션 수명)
        init_cfg = bubble_cfg.get("initiative") if isinstance(bubble_cfg, dict) else None
        init_cfg = init_cfg if isinstance(init_cfg, dict) else {}
        self._initiative = InitiativeEngine(
            self.root,
            init_cfg,
            is_screen_clear=self._bubble_screen_clear,
            seconds_since_activity=lambda: time.monotonic() - self._bubble_last_activity,
            show_nudge=self._initiative_show_nudge,
            phrase=make_persona_phraser(float(init_cfg.get("phrasing_timeout_sec", 25))),
            sources=default_sources(lambda: str(get_workdir()), scope_key="overlay"),
            on_outcome=self._on_initiative_outcome,
        )
        if self._chat_mode == "bubble":
            self._initiative.start()

        self._mcp_http_proc = self._start_mcp_http_server()

        # MCP server가 KuzuDB write lock을 획득할 때까지 대기한 후
        # dashboard를 시작해야 cross-process lock 충돌이 없다.
        threading.Thread(target=self._deferred_startup, daemon=True).start()

        # overlay 생존 중에는 MCP 리스너 상태를 감시하고,
        # 깨진 리스너(프로세스 생존 + 신규 연결 불가) 상태를 자동 복구한다.
        threading.Thread(target=self._mcp_health_monitor_loop, daemon=True, name="overlay-mcp-health").start()

        self._stm_server = STMServer(
            shutdown_callback=self._on_shutdown_request,
            new_session_callback=self.new_bubble_session,
        )
        self._stm_server.start()  # 포트 충돌 시 STMServer.start() 내부에서 조용히 실패

        # Global claude.exe discovery is intentionally disabled.  It cannot
        # distinguish Desktop Electron, safe-mode one-shots, or subagents.
        # Root CLI ownership is handled by the dedicated shim launcher.
        threading.Thread(target=self._stm_transcript_capture_loop, daemon=True, name="overlay-stm-capture").start()
        threading.Thread(target=self._auto_memory_checkpoint_loop, daemon=True, name="overlay-memory-checkpoint").start()

        keyboard.add_hotkey(hotkey, lambda: self.root.after(0, self._hotkey_chat))

        self.tray = _make_tray_icon(self)
        threading.Thread(target=self.tray.run, daemon=True).start()

        self._discord_bot = _try_start_discord_bot()

        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    @staticmethod
    def _report_callback_exception(exc, val, tb) -> None:
        """tk의 기본 report_callback_exception은 sys.stderr에 씀 — --noconsole 빌드는
        stderr가 없어서 콜백(after()/bind() 등) 안에서 터진 예외가 완전히 조용히
        사라진다. logging으로 보내야 세션 로그 파일에서 확인할 수 있다."""
        log.error("[overlay] tkinter 콜백 예외", exc_info=(exc, val, tb))

    @staticmethod
    def _coerce_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_mcp_health_settings(self) -> dict[str, float | int]:
        cfg = load_cfg()
        mcp_cfg = cfg.get("mcp") or {}
        return {
            "port": self._coerce_int(mcp_cfg.get("http_port", MCP_HTTP_PORT), MCP_HTTP_PORT),
            "transport": str(mcp_cfg.get("transport", "streamable-http") or "streamable-http").strip().lower(),
            "interval_secs": max(1.0, self._coerce_float(mcp_cfg.get("healthcheck_interval_secs", 5.0), 5.0)),
            "fail_threshold": max(1, self._coerce_int(mcp_cfg.get("healthcheck_fail_threshold", 3), 3)),
            "start_delay_secs": max(0.0, self._coerce_float(mcp_cfg.get("healthcheck_start_delay_secs", 15.0), 15.0)),
            "restart_cooldown_secs": max(1.0, self._coerce_float(mcp_cfg.get("restart_cooldown_secs", 8.0), 8.0)),
            "ready_timeout_secs": max(3.0, self._coerce_float(mcp_cfg.get("ready_timeout_secs", 20.0), 20.0)),
        }

    @staticmethod
    def _normalize_mcp_transport(value: str) -> str:
        transport = (value or "").strip().lower()
        if transport in {"sse", "streamable-http"}:
            return transport
        return "streamable-http"

    def _is_mcp_listener_ready(self, port: int, timeout: float = 1.0) -> bool:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
            conn.close()
            return True
        except OSError:
            return False

    def _is_mcp_http_healthy(self, port: int, timeout: float = 1.5) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(getattr(resp, "status", 0)) == 200
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False

    def _terminate_managed_process(self, attr_name: str, log_prefix: str, display_name: str, timeout: float = 5.0) -> bool:
        proc = getattr(self, attr_name, None)
        if not proc:
            return False
        if proc.poll() is not None:
            setattr(self, attr_name, None)
            return False

        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            proc.kill()
        setattr(self, attr_name, None)
        log.info("[%s] %s 종료", log_prefix, display_name)
        return True

    def _restart_managed_dependents(self) -> None:
        # mcp 재기동 후 종속 프로세스도 재연결을 보장하기 위해 재시작한다.
        self._terminate_managed_process("_dashboard_proc", "dashboard", "dashboard")
        self._terminate_managed_process("_kg_watcher_proc", "kg_watcher", "kg_watcher")
        self._kg_watcher_proc = self._start_kg_watcher()
        if self._is_dashboard_enabled():
            self._dashboard_proc = self._start_dashboard()

    def _recover_mcp_listener(self, reason: str) -> None:
        import time

        if self._quitting:
            return
        if not self._mcp_recovery_lock.acquire(blocking=False):
            return

        try:
            if self._quitting:
                return
            settings = self._get_mcp_health_settings()
            cooldown = float(settings["restart_cooldown_secs"])
            now = time.monotonic()
            if now - self._last_mcp_recovery_at < cooldown:
                return

            self._last_mcp_recovery_at = now
            port = int(settings["port"])
            log.warning("[mcp_http] 헬스체크 복구 시작: reason=%s port=%d", reason, port)

            self._terminate_managed_process("_mcp_http_proc", "mcp_http", "MCP HTTP 서버")
            self._mcp_http_proc = self._start_mcp_http_server()

            if self._quitting:
                return
            if self._wait_mcp_ready(timeout=float(settings["ready_timeout_secs"]), port=port):
                self._restart_managed_dependents()
                log.info("[mcp_http] 복구 완료: 리스너/종속 프로세스 재연결 보장")
            else:
                log.error("[mcp_http] 복구 실패: 포트 %d 리스너 준비 확인 실패", port)
        finally:
            self._mcp_recovery_lock.release()

    def _mcp_health_monitor_loop(self) -> None:
        import time

        start_delay = float(self._get_mcp_health_settings()["start_delay_secs"])
        if start_delay > 0:
            time.sleep(start_delay)

        failures = 0
        while not self._quitting:
            settings = self._get_mcp_health_settings()
            port = int(settings["port"])
            threshold = int(settings["fail_threshold"])
            interval = float(settings["interval_secs"])

            proc_dead = bool(self._mcp_http_proc and self._mcp_http_proc.poll() is not None)
            listener_ok = self._is_mcp_listener_ready(port)
            http_ok = self._is_mcp_http_healthy(port)
            healthy = listener_ok and http_ok and not proc_dead

            if healthy:
                failures = 0
            else:
                failures += 1
                log.warning(
                    "[mcp_http] health fail %d/%d (proc_dead=%s, listener_ok=%s, http_ok=%s, port=%d)",
                    failures,
                    threshold,
                    proc_dead,
                    listener_ok,
                    http_ok,
                    port,
                )
                if failures >= threshold:
                    if proc_dead:
                        reason = "proc_dead"
                    elif not listener_ok:
                        reason = "listener_unavailable"
                    else:
                        reason = "http_unhealthy"
                    self._recover_mcp_listener(reason=reason)
                    failures = 0

            sleep_left = interval
            while sleep_left > 0 and not self._quitting:
                step = min(0.5, sleep_left)
                time.sleep(step)
                sleep_left -= step

    def _claude_code_watchdog_loop(self) -> None:
        """Claude Code 프로세스 PID 감시 — 소멸 시 /stm/session/close 자동 트리거.

        전략:
        - PID 발견: 30초마다 PowerShell 1회 (두 쿼리를 하나로 합침)
        - 생사 확인: OpenProcess(0) — subprocess 없이 OS API 직접 호출
        - 메인 PID만 추적: node.exe 중 부모 PID가 일반 shell인 것만 (worker 제외)

        이 워치독은 TUI 모드(wt가 스폰해서 우리가 직접 프로세스를 소유하지 못하는 경로) 전용이다.
        bubble 모드의 claude 서브프로세스는 BubbleSessionManager가 직접 소유(자체 stop()으로 종료)하고,
        부모가 overlay.exe 자신이라 아래 쿼리의 shell 부모 필터(cmd/powershell/pwsh/wt/explorer)에
        걸리지 않는다 — 별도 배제 로직 없이도 겹치지 않는다(단독 스모크 테스트로 확인, 2026-07-21).
        """
        import time
        import ctypes
        import ctypes.wintypes

        _kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000

        def _is_pid_alive(pid: int) -> bool:
            h = _kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            _kernel32.CloseHandle(h)
            return True

        def _find_claude_code_pids() -> set[int]:
            """Claude Code 루트 PID만 수집 — 부모가 shell인 node.exe만 (worker 제외).

            워커 프로세스는 부모가 node.exe이므로 제외.
            claude.exe는 단일 프로세스이므로 전부 포함.
            """
            query = (
                "$shells = @('cmd.exe','powershell.exe','pwsh.exe','wt.exe','WindowsTerminal.exe','explorer.exe');"
                "$allProcs = @{};"
                "Get-CimInstance Win32_Process | ForEach-Object { $allProcs[$_.ProcessId] = $_.Name };"
                "$pids = @();"
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                "Where-Object { $_.CommandLine -like '*claude*' } | "
                "Where-Object { $shells -contains $allProcs[$_.ParentProcessId] } | "
                "ForEach-Object { $pids += $_.ProcessId };"
                "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                "ForEach-Object { $pids += $_.ProcessId };"
                "$pids"
            )
            try:
                result = subprocess.run(
                    ["powershell", "-Command", query],
                    capture_output=True, text=True, timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                pids = set()
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
                return pids
            except Exception:
                return set()

        def _trigger_close_session(dead_pid: int) -> None:
            try:
                port = self._stm_server.port
                payload = json.dumps({
                    "summary": f"[watchdog] Claude Code PID {dead_pid} 종료 감지 — 자동 close_session",
                    "scope_key": "overlay",
                }).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/stm/session/close",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=70) as resp:
                    body = resp.read().decode()
                    log.info("[watchdog] close_session 완료 (PID=%d): %s", dead_pid, body)
            except Exception as exc:
                log.warning("[watchdog] close_session 실패 (PID=%d): %s", dead_pid, exc)

            # 페르소나 반성 이벤트 감지 — resume 가능한 Claude 세션(기존 CLI 인증 재사용,
            # 새 API 키 불필요)으로 판단만 하고, narrative/persona는 절대 여기서 안 건드림.
            # 감지되면 curiosity로 남겨 다음 실제 대화 세션의 자율 판단에 맡김.
            try:
                from core.graph.semantic import flag_reflection_event_from_recent_session

                flag_reflection_event_from_recent_session(scope_key="overlay")
            except Exception as exc:
                log.warning("[watchdog] reflection event 감지 실패 (PID=%d): %s", dead_pid, exc)

        # STM 서버 준비 대기
        time.sleep(5.0)

        known_pids: set[int] = set()
        discover_interval = 30.0   # PowerShell 발견: 30초마다
        alive_interval = 5.0       # OS API 생사 확인: 5초마다
        last_discover = 0.0

        while not self._quitting:
            now = time.monotonic()

            # 주기적 발견 (PowerShell 1회)
            if now - last_discover >= discover_interval:
                current_pids = _find_claude_code_pids()
                new_pids = current_pids - known_pids
                if new_pids:
                    log.info("[watchdog] Claude Code PID 신규 감지: %s", new_pids)
                    known_pids |= new_pids
                last_discover = now

            # 기존 추적 PID 생사 확인 (OS API, subprocess 없음)
            dead_pids = {pid for pid in known_pids if not _is_pid_alive(pid)}
            for pid in dead_pids:
                log.info("[watchdog] Claude Code PID %d 소멸 → close_session 트리거", pid)
                threading.Thread(
                    target=_trigger_close_session, args=(pid,), daemon=True
                ).start()
            known_pids -= dead_pids

            sleep_left = alive_interval
            while sleep_left > 0 and not self._quitting:
                time.sleep(min(0.5, sleep_left))
                sleep_left -= 0.5

    def _stm_transcript_capture_loop(self) -> None:
        """Claude Code transcript(.jsonl)를 LLM 호출 없이 tail하여 STM에 자동 반영한다.

        engram_save_message가 모델의 자발적 tool-call에 의존해 불안정한 문제(MCP 경로)를
        보완하기 위해, 디스크에 이미 남는 세션 로그를 incremental하게 읽어 user/assistant의
        최종 텍스트만 룰 기반으로 추출 + 민감정보 마스킹 후 /stm/message로 전달한다.
        """
        import time
        from core.memory.transcript_capture import (
            claude_project_dir,
            find_active_transcript,
            read_new_lines,
            extract_turns,
            redact_secrets,
        )

        time.sleep(5.0)

        project_dir = claude_project_dir(str(Path.cwd()))
        current_path: "Path | None" = None
        offset = 0
        poll_interval = 5.0

        def _post_message(role: str, text: str, request_id: str) -> None:
            try:
                port = self._stm_server.port
                payload = json.dumps({
                    "scope_key": "overlay",
                    "role": role,
                    "content": text,
                    "request_id": request_id,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/stm/message",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read()
            except Exception as exc:
                log.warning("[stm-capture] /stm/message 전송 실패: %s", exc)

        while not self._quitting:
            try:
                active = find_active_transcript(project_dir)
                if active is not None and active != current_path:
                    log.info("[stm-capture] 활성 transcript 전환: %s", active.name)
                    current_path = active
                    offset = 0

                if current_path is not None and current_path.exists():
                    new_offset, lines = read_new_lines(current_path, offset)
                    turns = extract_turns(lines)
                    for i, (role, text) in enumerate(turns):
                        safe_text = redact_secrets(text)
                        request_id = f"{current_path.name}:{offset}:{i}"
                        _post_message(role, safe_text, request_id)
                    offset = new_offset
            except Exception as exc:
                log.warning("[stm-capture] capture loop 오류: %s", exc)

            sleep_left = poll_interval
            while sleep_left > 0 and not self._quitting:
                time.sleep(min(0.5, sleep_left))
                sleep_left -= 0.5

    def _auto_memory_checkpoint_loop(self) -> None:
        """유휴 세션을 닫지 않고 working/LTM/daily checkpoint를 갱신한다."""
        import time

        time.sleep(30.0)
        while not self._quitting:
            try:
                from core.config.runtime_config import get_cfg_value

                if bool(get_cfg_value("memory.auto_checkpoint.enabled", True)) and not self._bubble_turn_active:
                    from core.graph.semantic import maybe_auto_checkpoint

                    result = maybe_auto_checkpoint(
                        scope_key="overlay",
                        cwd=str(get_workdir()),
                        idle_seconds=int(
                            get_cfg_value("memory.auto_checkpoint.idle_seconds", 1800)
                        ),
                        min_user_turns=int(
                            get_cfg_value("memory.auto_checkpoint.min_user_turns", 5)
                        ),
                        external_daily_dir=str(
                            get_cfg_value(
                                "memory.auto_checkpoint.external_daily_dir",
                                "",
                            )
                            or ""
                        ),
                    )
                    if result.get("status") not in {"skipped", "busy"}:
                        log.info("[auto-checkpoint] %s", result)
                poll_seconds = max(
                    30,
                    int(
                        get_cfg_value(
                            "memory.auto_checkpoint.poll_seconds",
                            60,
                        )
                    ),
                )
            except Exception as exc:
                log.warning("[auto-checkpoint] loop 실패: %s", exc)
                poll_seconds = 60

            sleep_left = poll_seconds
            while sleep_left > 0 and not self._quitting:
                time.sleep(min(0.5, sleep_left))
                sleep_left -= 0.5

    def _start_mcp_http_server(self) -> "subprocess.Popen | None":
        """Copilot/Gemini CLI를 위한 지속 MCP HTTP(SSE) 서버를 overlay 수명에 맞춰 시작한다."""
        cfg = load_cfg()
        mcp_cfg = cfg.get("mcp") or {}
        port = int(mcp_cfg.get("http_port", MCP_HTTP_PORT))
        transport = self._normalize_mcp_transport(str(mcp_cfg.get("transport", "streamable-http") or "streamable-http"))
        # 이미 포트가 열려있으면 외부 MCP 서버 재사용 (dev_backend 등)
        if self._is_mcp_listener_ready(port, timeout=1.0):
            if self._is_mcp_http_healthy(port, timeout=1.5):
                log.info("[mcp_http] 포트 %d 이미 응답 중 — 외부 MCP 서버 재사용, 시작 스킵", port)
                return None
            log.warning("[mcp_http] 포트 %d 리스너는 있으나 /health 실패 — 신규 MCP 서버 기동 시도", port)
        # frozen 번들: 같은 exe 를 멀티콜(`--role mcp-server`)로 재실행 → conda python 불필요.
        # 개발(소스) 모드: conda python 으로 mcp_server.py 실행.
        # 원격 인증 리스너는 명시적으로 켤 때만 연다. SSH 리버스 터널은 http_port 가
        # 아니라 이 포트에 연결한다 — http_port 는 무인증이므로 터널에 노출하면 안 된다.
        remote_args: list[str] = []
        if bool(mcp_cfg.get("remote_enabled", False)):
            remote_port = int(mcp_cfg.get("remote_port", port + 1))
            remote_args = ["--remote-port", str(remote_port)]
            log.info("[mcp_http] 원격 인증 리스너 활성 port=%d", remote_port)

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--role", "mcp-server", "--transport", transport, "--port", str(port), *remote_args]
            cwd = str(PROJECT_ROOT)
        else:
            py = _find_mcp_python()
            script = _find_mcp_script()
            if not py or not script:
                log.warning("[mcp_http] Python 또는 mcp_server.py를 찾을 수 없어 MCP HTTP 서버 시작 스킵")
                return None
            cmd = [py, str(script), "--transport", transport, "--port", str(port), *remote_args]
            cwd = str(script.parent)
        try:
            from core.config.runtime_config import get_db_root_dir

            env = os.environ.copy()
            env["ENGRAM_DB_DIR"] = get_db_root_dir()
            env["ENGRAM_RUNTIME_MODE"] = "frozen" if getattr(sys, "frozen", False) else "source"
            env["ENGRAM_RUNTIME_PARENT_PID"] = str(os.getpid())
            env["ENGRAM_RUNTIME_SOURCE_ROOT"] = "" if getattr(sys, "frozen", False) else str(PROJECT_ROOT.resolve())
            # overlay 역할 해제 — MCP 서버는 KuzuDB 직접 접근 가능
            env.pop("ENGRAM_RUNTIME_ROLE", None)
            log_path = Path.home() / ".engram" / "mcp-http.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log.info("[mcp_http] MCP HTTP 서버 시작 PID=%d port=%d transport=%s", proc.pid, port, transport)
            return proc
        except Exception as exc:
            log.warning("[mcp_http] MCP HTTP 서버 시작 실패: %s", exc)
            return None

    def _wait_mcp_ready(self, timeout: float = 15.0, port: int | None = None) -> bool:
        """MCP server가 /health 에 응답할 때까지 대기. 성공 시 True."""
        import time

        if port is None:
            cfg = load_cfg()
            port = int((cfg.get("mcp") or {}).get("http_port", MCP_HTTP_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_mcp_listener_ready(port, timeout=1.0) and self._is_mcp_http_healthy(port, timeout=1.5):
                log.info("[mcp_http] MCP server ready (port=%d)", port)
                return True
            time.sleep(0.5)
        log.warning("[mcp_http] MCP server not ready after %.1fs", timeout)
        return False

    def _deferred_startup(self) -> None:
        """MCP server 준비 완료 후 kg_watcher / dashboard를 순서대로 시작한다."""
        settings = self._get_mcp_health_settings()
        mcp_ready = self._wait_mcp_ready(
            timeout=float(settings["ready_timeout_secs"]), port=int(settings["port"])
        )
        self._kg_watcher_proc = self._start_kg_watcher()
        if self._is_dashboard_enabled():
            self._dashboard_proc = self._start_dashboard()
        # 원격 리스너가 떠 있어야 터널이 의미가 있으므로 MCP 준비 이후에 연다.
        self._apply_tunnels(listener_ready=mcp_ready)
        # 전역 SessionStart hook 을 현재 auto_inject 설정과 동기화한다(설치/제거 멱등).
        try:
            sync_sessionstart_hook(is_auto_inject_enabled())
        except Exception:
            log.exception("[overlay] SessionStart hook 동기화 실패")
        guidance_enabled = is_policy_guidance_enabled()
        for name, result in (
            ("policy marker", sync_policy_guidance_disabled_marker(guidance_enabled)),
            ("Claude PreToolUse", sync_claude_pretool_hook(guidance_enabled)),
            ("Codex PreToolUse", sync_codex_pretool_hook(guidance_enabled)),
            ("Copilot PreToolUse", sync_copilot_pretool_hook(guidance_enabled)),
            ("Gemini BeforeTool", sync_gemini_pretool_hook(guidance_enabled)),
        ):
            if not result.get("ok"):
                log.error("[overlay] %s 동기화 실패: %s", name, result.get("error", "unknown error"))

    def _apply_tunnels(self, listener_ready: bool | None = None) -> None:
        """설정의 터널 목록을 복원하고, 허용된 경우 키 인증으로 자동 연결한다."""
        try:
            cfg = load_cfg()
            mcp_cfg = cfg.get("mcp") or {}
            if not bool(mcp_cfg.get("remote_enabled", False)):
                self._tunnels.stop_all()
                return
            hosts = [
                str(t.get("host")).strip()
                for t in (mcp_cfg.get("tunnels") or [])
                if isinstance(t, dict) and t.get("host")
            ]
            self._tunnels.register(hosts)
            auto_reconnect = bool(mcp_cfg.get("tunnel_auto_reconnect", False))
            if not auto_reconnect:
                if hosts:
                    log.info("[tunnel] 대상 %d개 복원(자동 재연결 꺼짐): %s", len(hosts), ", ".join(hosts))
                return

            remote_port = int(mcp_cfg.get("remote_port", 17386) or 17386)
            if listener_ready is False or not self._is_mcp_listener_ready(remote_port, timeout=1.0):
                log.warning("[tunnel] 원격 리스너가 준비되지 않아 자동 연결 생략 (port=%d)", remote_port)
                return

            for host in hosts:
                self._tunnels.start_automatic(host)
            if hosts:
                log.info("[tunnel] 대상 %d개 자동 연결 요청: %s", len(hosts), ", ".join(hosts))
        except Exception:
            log.exception("[tunnel] 목록 복원 실패")

    def _start_dashboard(self) -> "subprocess.Popen | None":
        """Streamlit dashboard를 시작한다."""
        import subprocess as _sp

        cfg = load_cfg()
        dashboard_cfg = cfg.get("dashboard") if isinstance(cfg, dict) else None
        dashboard_cfg = dashboard_cfg if isinstance(dashboard_cfg, dict) else {}
        port = self._coerce_int(dashboard_cfg.get("port", 8501), 8501)
        if getattr(sys, "frozen", False):
            dashboard_exe = Path(sys.executable).parent / "engram-dashboard.exe"
            if not dashboard_exe.is_file():
                log.warning("[dashboard] sidecar 없음: %s", dashboard_exe)
                return None
            cmd = [str(dashboard_exe), "--port", str(port)]
            cwd = str(Path(sys.executable).parent)
        else:
            py = _find_mcp_python()
            if not py:
                log.warning("[dashboard] Python을 찾을 수 없어 시작 스킵")
                return None
            script = (PROJECT_ROOT / "scripts" / "engram_dashboard.py").resolve()
            if not script.exists():
                log.warning("[dashboard] 스크립트 없음: %s", script)
                return None
            streamlit_exe = Path(py).parent / "streamlit"
            if streamlit_exe.exists() or Path(str(streamlit_exe) + ".exe").exists():
                cmd = [str(streamlit_exe), "run", str(script), "--server.headless", "true", "--server.port", str(port)]
            else:
                cmd = [py, "-m", "streamlit", "run", str(script), "--server.headless", "true", "--server.port", str(port)]
            cwd = str(script.parent.parent)

        try:
            log_path = Path.home() / ".engram" / "dashboard.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            proc = _sp.Popen(
                cmd,
                cwd=cwd,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            log.info("[dashboard] 시작 PID=%d", proc.pid)
            return proc
        except Exception as exc:
            log.warning("[dashboard] 시작 실패: %s", exc)
            return None

    @staticmethod
    def _is_dashboard_enabled(cfg: dict | None = None) -> bool:
        if cfg is None:
            cfg = load_cfg()
        dashboard_cfg = cfg.get("dashboard") if isinstance(cfg, dict) else None
        if not isinstance(dashboard_cfg, dict):
            return True
        return bool(dashboard_cfg.get("enabled", True))

    def _start_kg_watcher(self) -> "subprocess.Popen | None":
        """MCP server 준비 완료 후 kg_watcher를 overlay 자식 프로세스로 시작한다."""
        import subprocess as _sp

        # frozen 번들: 같은 exe 를 `--role kg-watcher` 로 재실행. 소스: conda python 으로 스크립트 실행.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--role", "kg-watcher"]
            cwd = str(PROJECT_ROOT)
        else:
            py = _find_mcp_python()
            if not py:
                log.warning("[kg_watcher] Python을 찾을 수 없어 시작 스킵")
                return None
            script = (PROJECT_ROOT / "scripts" / "kg" / "kg_watcher.py").resolve()
            if not script.exists():
                log.warning("[kg_watcher] 스크립트 없음: %s", script)
                return None
            cmd = [py, str(script)]
            cwd = str(script.parent.parent.parent)

        try:
            log_path = Path.home() / ".engram" / "kg-watcher.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            proc = _sp.Popen(
                cmd,
                cwd=cwd,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            log.info("[kg_watcher] 시작 PID=%d", proc.pid)
            return proc
        except Exception as exc:
            log.warning("[kg_watcher] 시작 실패: %s", exc)
            return None

    def restart(self):
        """overlay 프로세스를 재시작한다 (자신을 재실행)."""
        self._quit_reason = "restart_request"
        log.info("[overlay] 재시작 요청")
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            entrypoint = (PROJECT_ROOT / "engram_overlay_entry.py").resolve()
            cmd = [sys.executable, str(entrypoint)]
        cwd = str(PROJECT_ROOT)

        # root.after() 는 root.destroy() 시 취소되므로 threading.Timer 사용
        def _spawn():
            subprocess.Popen(cmd, cwd=cwd)

        threading.Timer(0.5, _spawn).start()
        self.request_quit()

    def request_quit(self):
        """다른 스레드에서도 안전하게 종료를 요청한다."""
        try:
            self.root.after(0, self.quit)
        except Exception:
            self.quit()

    def open_settings(self):
        """설정 GUI 창을 열고 저장 후 config를 다시 로드한다."""
        open_settings(
            self.root,
            on_saved=self._reload_config,
            on_get_ollama_models=_get_ollama_model_list_snapshot,
            on_reload_ollama_models=_reload_ollama_models,
            tunnels=self._tunnels,
        )

    def _reload_config(self):
        """설정 저장 후 overlay config를 다시 읽어 반영한다."""
        # external_renderer is intentionally start-time only.  A new selection
        # takes effect after a full overlay restart, preserving handshake and
        # bundled-renderer fallback ownership in the lifecycle startup path.
        log.info("[overlay] external renderer selection is restart-only; keeping current renderer lifecycle")
        # CharacterOverlay owns image/profile caches, so saving from the GUI must
        # replace them now rather than waiting for the filesystem watcher tick.
        if not self.character.reload_config():
            log.warning("[overlay] settings reload rejected; keeping last good runtime configuration")
            return
        cfg = load_cfg()
        new_provider = get_cli_provider(cfg)
        self._chat_mode = get_chat_mode(cfg)
        self.character.set_flip(get_flip_horizontal(cfg))
        # 터널 목록/자동 여부 변경을 즉시 반영한다.
        self._apply_tunnels()
        # 기존 세션은 시작 시점 컨텍스트(페르소나/권한 수준)를 유지하므로,
        # 설정 저장 후에는 세션을 닫아 다음 채팅에서 최신 설정을 적용한다.
        self.chat.kill()
        if self._bubble_session is not None:
            self._bubble_session.stop()
            self._bubble_session = None
        # Config reload must preserve legacy claude-code + ollama_model routing.
        self._set_provider_model(new_provider, None if new_provider == "claude-code" else get_cli_model(new_provider, cfg))

        # 말풍선 색상 테마 등 — 재시작 없이 바로 반영(안 그러면 다음 프로세스 재시작까지 안 보임).
        bubble_cfg = get_bubble_cfg(cfg)
        self._bubble_manager.update_cfg(bubble_cfg)
        self._bubble_input.update_cfg(bubble_cfg)

        if self._is_dashboard_enabled(cfg):
            if self._dashboard_proc is None or self._dashboard_proc.poll() is not None:
                self._dashboard_proc = self._start_dashboard()
        else:
            self._terminate_managed_process("_dashboard_proc", "dashboard", "dashboard")

        # 능동적 상주 설정 반영 + 모드에 따라 루프 on/off.
        init_cfg = bubble_cfg.get("initiative") if isinstance(bubble_cfg, dict) else None
        self._initiative.update_cfg(init_cfg if isinstance(init_cfg, dict) else {})
        if self._chat_mode == "bubble":
            self._initiative.start()
        else:
            self._initiative.stop()

    def get_cli_provider(self) -> str:
        return self._cli_provider

    def get_cli_model(self, provider: str) -> str:
        return get_cli_model(provider)

    def set_cli_provider(self, provider: str):
        """character overlay 우클릭 등 외부 콜백 호환용."""
        self._set_provider_model(provider, None)

    def _set_provider_model(self, provider: str, model: str | None):
        """provider와 ollama_model을 원자적으로 업데이트한다.
        model=None 이면 현재 모델 유지, model='' 이면 클리어.
        """
        normalized = set_cli_provider(provider, sync_user=True)
        if model is not None:
            set_cli_model(normalized, model, sync_user=True)
            # Empty model is the explicit "claude (direct)" menu choice.  Clear
            # the historic fallback too; otherwise it would silently route local.
            if normalized == "claude-code" and model == "":
                self._ollama_model = set_ollama_model("", sync_user=True)
            if normalized in {"ollama", "claude-code-ollama"}:
                self._ollama_model = set_ollama_model(model, sync_user=True)
        self._cli_provider = normalized
        self.chat.set_provider(normalized)
        try:
            self.tray.title = f"engram overlay ({normalized})"
        except Exception:
            pass
        log.info("[overlay] provider=%s model=%s", normalized, model)

    def toggle_chat(self):
        if self._chat_mode == "bubble":
            self._toggle_bubble_input()
        else:
            self._ensure_tui_mode()
            x, y, w, h = self._get_bubble_anchor_rect()
            self.chat.show_at_overlay(x, y, w, h)

    def _get_bubble_anchor_rect(self) -> tuple[int, int, int, int]:
        """Resolve one shared bubble session's visual source on every layout."""
        if self._overlay_events.mode == "replace":
            return self.character.get_phys_rect()
        if self._bubble_anchor == "observer" and self._observer_rect is not None:
            return self._observer_rect
        return self.character.get_bundled_phys_rect()

    def _select_bubble_anchor(self, source: str) -> None:
        if source == "observer" and self._observer_rect is None:
            return
        self._bubble_anchor = source
        # Reflow existing speech/thought bubbles as well as the next input.
        if hasattr(self, "_bubble_manager"):
            self._bubble_manager.refresh_positions()
        if hasattr(self, "_bubble_input"):
            self._bubble_input.refresh_position()
        if hasattr(self, "_bubble_history"):
            self._bubble_history.refresh_position()

    def _hotkey_chat(self):
        if self._chat_mode == "bubble":
            self._toggle_bubble_input()
        else:
            self._ensure_tui_mode()
            self.chat.show_at_cursor()

    def _ensure_tui_mode(self) -> None:
        """TUI 세션을 열기 전, 같은 scope_key="overlay"를 쓰는 bubble 세션을 먼저 정리한다."""
        if self._bubble_session is not None:
            self._bubble_session.stop()
            self._bubble_session = None

    def _ensure_bubble_session(self) -> None:
        """bubble 세션을 열기 전, 같은 scope_key="overlay"를 쓰는 TUI 세션을 먼저 정리한다.

        세션 자체는 캐릭터를 처음 클릭할 때 지연 기동한다(TUI가 클릭해야 wt를 띄우는 것과 동일한 철학).
        """
        self.chat.kill()
        if self._bubble_session is not None and self._bubble_session.is_alive():
            return
        cfg = load_cfg()
        bubble_cfg = get_bubble_cfg(cfg)
        workdir = str(get_workdir(cfg))
        self._bubble_session = BubbleSessionManager(
            cwd=workdir,
            env_overrides={"ENGRAM_SCOPE_KEY": "overlay", "ENGRAM_CLI_PROVIDER": "claude-code"},
            permission_level=get_permission_level(cfg),
            on_event=lambda ev: self.root.after(0, lambda ev=ev: self._on_bubble_event(ev)),
            on_approval_request=lambda req: self.root.after(0, lambda req=req: self._bubble_manager.show_approval_request(req)),
            resume_session_id=get_bubble_session_id(),
            on_session_id=lambda sid: set_bubble_session_id(sid),
            stm_bridge=StmBridge(scope_key="overlay"),
            # 확장 사고 예산 — 생각풍선에 실제 추론 텍스트를 보여주기 위함(0이면 끔).
            thinking_tokens=int(bubble_cfg.get("thinking_tokens", 2000)),
            # TUI 셔임과 동일하게 auto_inject와 무관하게 항상 부트스트랩 지시문을 덧댄다 —
            # 그래야 기본 chat_mode(bubble)로 시작하는 신규 사용자도 튜토리얼 안내를 받는다.
            bootstrap_prompt=bubble_bootstrap_prompt(workdir),
        )
        self._bubble_session.start()

    def _toggle_bubble_input(self) -> None:
        # 캐릭터를 옮긴 뒤 다시 클릭한 경우 — 이전 응답의 말풍선/생각풍선이 아직 떠 있다면
        # 캐릭터의 새 위치를 따라가게 다시 배치한다(콘텐츠 이벤트 없이는 저절로 안 움직임).
        self._bubble_manager.refresh_positions()
        if self._bubble_input.is_showing():
            self._bubble_input.hide()
            return
        self._ensure_bubble_session()
        # 클릭으로 입력창을 열 때, 페이드로 사라진 마지막 교환(응답 +/- 질문 에코)을
        # 되살려서 "방금 뭐였지" 를 바로 다시 볼 수 있게 한다.
        self._bubble_manager.replay_last()
        self._bubble_input.show(on_submit=self._on_bubble_submit)

    def _on_bubble_submit(self, text: str) -> None:
        # 내 메시지는 입력창이 있던 자리에 "에코 말풍선"으로 잠깐 남긴다(응답과 별개).
        # 응답 말풍선은 입력창과 무관하게 자기 위치(마지막 드래그 위치 또는 캐릭터 옆
        # 상단 기본)에 별도로 뜬다 — 내 입력이 응답으로 출력되는 것처럼 보이던 문제 해결.
        self._bubble_last_activity = time.monotonic()
        self.character.notify_input()
        self._overlay_events.publish("conversation.input_submitted", "input")
        self._overlay_events.publish("generation.started", "generating")
        self._bubble_turn_active = True  # 턴 시작 — 응답이 끝날(turn_end/error/result) 때까지 발화 억제
        # 자율발화에 대한 답장일 때만 engaged 로 친다. 예전엔 모든 입력에서 무조건
        # notify_engaged 를 불렀는데, 그러면 자율발화와 무관한 평소 대화가 백오프를
        # 계속 0 으로 되돌려서 "무시가 쌓이면 뜸해진다"는 규칙이 사실상 죽어 있었다.
        if self._nudge_awaiting_reply:
            self._nudge_awaiting_reply = False
            if self._nudge_engage_live:
                self._initiative.notify_engaged()
            else:
                self._initiative.notify_late_engaged()
        self._bubble_manager.show_echo(text, self._bubble_input.get_last_rect())
        self._bubble_manager.show_user_message(text)
        if self._bubble_session is not None:
            self._bubble_session.send(text)

    def _on_bubble_event(self, ev: dict) -> None:
        """세션 이벤트를 렌더러로 넘기면서, initiative 발화 판정에 필요한 상태
        (마지막 활동 시각 + 턴 진행 여부)를 갱신한다."""
        self._bubble_last_activity = time.monotonic()
        kind = ev.get("kind") if isinstance(ev, dict) else None
        if kind in ("turn_end", "error", "result"):
            self._bubble_turn_active = False
        self._initiative.feed_event(ev)
        self._overlay_events.publish_bubble(ev)
        self._bubble_manager.handle_event(ev)
        try:
            self.character.handle_bubble_event(ev)
        except Exception:
            log.debug("[overlay] character state event skipped", exc_info=True)

    def _restore_bundled_renderer(self) -> None:
        """Replacement failure must always return control to the bundled window."""
        try:
            self.character.restore_bundled_renderer()
            self._observer_rect = None
            self._select_bubble_anchor("bundled")
        except Exception:
            log.debug("[overlay-api] bundled renderer restore skipped", exc_info=True)
        finally:
            self._replace_startup_geometry_pending = False

    def _on_bundled_pointer_event(self, action: str, payload: dict) -> None:
        """Expose host-owned pointer semantics to observer renderers."""
        if action in {"left_click", "right_click"}:
            self._select_bubble_anchor("bundled")
        mapping = {
            "pointer_enter": ("pointer.entered", "hover"),
            "pointer_leave": ("pointer.left", "idle"),
            "left_click": ("pointer.left_clicked", "click"),
            "right_click": ("pointer.right_clicked", "click"),
            "drag_move": ("overlay.position_changed", "idle"),
            "drag_end": ("overlay.position_changed", "idle"),
        }
        event = mapping.get(action)
        if event is not None:
            self._overlay_events.publish(event[0], event[1], payload)

    def _drain_external_renderer_messages(self) -> None:
        """Run renderer callbacks on Tk's event loop, never on the stdout thread."""
        try:
            while True:
                message = self._renderer_inbound.get_nowait()
                if message.get("type") == "_renderer.failure":
                    self._restore_bundled_renderer()
                else:
                    self._handle_external_renderer_message(message)
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(50, self._drain_external_renderer_messages)

    def _handle_external_renderer_message(self, message: dict) -> None:
        """Tk-thread handler for the small, public renderer-to-host input contract."""
        if message.get("schema_version") not in (None, 1):
            return
        payload = message.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        kind = message.get("type")
        try:
            if kind == "overlay.geometry_changed":
                if self._overlay_events.mode == "observer":
                    width, height = max(1, int(payload["width"])), max(1, int(payload["height"]))
                    x, y = int(payload["x"]), int(payload["y"])
                    self._observer_rect = (x, y, width, height)
                    if self._bubble_anchor == "observer":
                        self._bubble_manager.refresh_positions()
                else:
                    startup_geometry = getattr(self, "_replace_startup_geometry_pending", False)
                    rect = self.character.apply_external_geometry(
                        payload["x"], payload["y"], payload["width"], payload["height"],
                        preserve_position=startup_geometry,
                    )
                    self._replace_startup_geometry_pending = False
                    self._overlay_events.publish(
                        "overlay.set_position", "idle", {"x": rect[0], "y": rect[1]}
                    )
            elif kind == "pointer.action":
                action = payload.get("action")
                if action == "left_click":
                    self._overlay_events.publish("pointer.left_clicked", "click")
                    if self._overlay_events.mode == "observer":
                        if self._observer_rect is None:
                            log.warning("[overlay-api] ignored observer click without geometry")
                            return
                        self._select_bubble_anchor("observer")
                    self.character.external_activate()
                elif action == "right_click":
                    self._overlay_events.publish("pointer.right_clicked", "click")
                    if self._overlay_events.mode == "observer":
                        if self._observer_rect is None:
                            log.warning("[overlay-api] ignored observer context click without geometry")
                            return
                        self._select_bubble_anchor("observer")
                    self.character.external_context_menu(int(payload["screen_x"]), int(payload["screen_y"]))
                elif action == "pointer_enter":
                    self._overlay_events.publish("pointer.entered", "hover")
                elif action == "pointer_leave":
                    self._overlay_events.publish("pointer.left", "idle")
                elif action in {"drag_move", "drag_end"}:
                    if self._overlay_events.mode == "observer":
                        return
                    rect = self.character.get_phys_rect()
                    rect = self.character.apply_external_geometry(
                        int(payload["screen_x"]), int(payload["screen_y"]), rect[2], rect[3]
                    )
                    self._overlay_events.publish(
                        "overlay.set_position", "idle", {"x": rect[0], "y": rect[1]}
                    )
            elif kind == "overlay.heartbeat":
                return
        except (KeyError, TypeError, ValueError):
            log.warning("[overlay-api] invalid external renderer input: %s", kind)

    def _bubble_screen_clear(self) -> bool:
        """지금 능동 발화를 띄워도 되는가 — bubble 모드 · 입력창 닫힘 · 턴 진행 안 함 ·
        떠 있는 풍선 없음일 때만 True.

        막힌 이유가 바뀔 때만 로그로 남긴다 — 엔진 쪽 메시지는 "화면이 비어있지 않음"
        까지만 알 수 있어서, 넷 중 무엇인지는 여기서만 구분된다.
        (speech_fade:false 로 응답 풍선이 계속 떠 있으면 영구히 막히는데, 그게
        로그에 안 남으면 밖에서는 원인을 알 수 없다.)"""
        if self._chat_mode != "bubble":
            reason = f"chat_mode={self._chat_mode}"
        elif self._bubble_turn_active:
            reason = "턴 진행 중"
        elif self._bubble_input.is_showing():
            reason = "입력창 열림"
        elif not self._bubble_manager.is_idle():
            reason = "떠 있는 풍선 있음(speech_fade 설정 확인)"
        else:
            reason = ""
        if reason != getattr(self, "_screen_block_reason", None):
            self._screen_block_reason = reason
            if reason:
                log.info("[overlay] 자율발화 화면 조건 미충족: %s", reason)
        return not reason

    def _initiative_show_nudge(self, text: str, engine_on_click) -> None:
        """엔진이 만든 능동 발화를 말풍선으로 렌더한다. 클릭하면 엔진에 알린 뒤(백오프
        리셋) 입력창을 열어 대화로 잇는다."""
        dwell = None
        cfg_init = getattr(self._initiative, "_cfg", {}) or {}
        try:
            dwell = int(cfg_init.get("nudge_dwell_ms")) if cfg_init.get("nudge_dwell_ms") else None
        except Exception:
            dwell = None

        def _click():
            try:
                engine_on_click()
            finally:
                self._engage_nudge()

        self._bubble_manager.show_nudge(
            text, _click, dwell_ms=dwell, on_ignored=self._initiative.notify_ignored,
        )

    def _engage_nudge(self) -> None:
        """자율발화 풍선(또는 답장 아이콘)을 눌렀다 — **입력창을 연다.**

        예전에는 소재별 `engage_prompt` 를 그대로 제출했고(1차), 그다음엔 입력창에
        초안으로 채웠다(2차). 둘 다 "내 의견 없이 문장이 자동 생성된다"는 같은 문제라
        결국 그 필드를 없앴다. 답장 버튼의 계약은 "눌러서 **빈** 입력창이 뜨는 것"이다.

        입력창은 **비워서** 연다. 문구를 미리 채워두는 것도 결국 "내 의견 없이 문장이
        자동 생성되는" 것이라 답장의 의미가 없다. 발화 문구는 세션에 보낼 때 앞에
        prepend 되므로(_with_nudge_context) 짧게 "응 해줘"만 써도 맥락이 통한다.

        여기서 결과를 engaged 로 확정하지 않는다. 입력창을 열어준 것뿐이고, 실제로
        보냈는지(_on_bubble_submit)와 그냥 닫았는지(_on_nudge_input_closed)를 구분해야
        "열어는 봤는데 할 말이 없더라"가 참여로 둔갑하지 않는다."""
        self._bubble_last_activity = time.monotonic()
        # 화면에 실제로 떠 있던 문구 — 프레이징을 거쳤으면 템플릿이 아니라 그 문장이다.
        nudge_text = self._initiative.active_nudge_text()
        self._ensure_bubble_session()
        self._bubble_manager.refresh_positions()
        # 열려 있던 입력창부터 닫는다. 이 hide() 는 직전 발화의 닫힘 콜백을 소비하므로
        # 반드시 _nudge_awaiting_reply 를 새 값으로 세우기 **전에** 끝나야 한다 —
        # 순서가 뒤집히면 이전 입력창이 닫히면서 방금 뜬 발화를 무응답으로 마감해버린다.
        if self._bubble_input.is_showing():
            self._bubble_input.hide()
        # 살아있는 발화(판정 대기)에 답하는 건지, 이미 판정이 끝난 지난 발화를 되살려
        # 뒤늦게 답하는 건지 구분해둔다 — 전자는 engaged, 후자는 late_engaged 로
        # 기록이 갈린다(이미 기록된 ignored 를 소급 수정하지 않기 위함).
        self._nudge_engage_live = self._initiative.has_pending_outcome()
        self._nudge_awaiting_reply = True
        self._pending_nudge_text = nudge_text
        self._bubble_input.show(
            on_submit=self._on_nudge_reply_submit,
            on_close=self._on_nudge_input_closed,
        )

    @staticmethod
    def _with_nudge_context(nudge_text: str, user_text: str) -> str:
        """세션에 보낼 첫 프롬프트 앞에 방금 건넨 말을 얹는다.

        이게 없으면 캐릭터가 A 라고 말을 걸어 사용자가 거기에 답했는데 세션은 A 를
        본 적이 없어서 맥락 없는 답을 한다 — 사용자 눈엔 "자기가 한 말을 기억 못 하는"
        것으로 보인다. 상주 세션의 첫 프롬프트에만 붙으면 되고, 이후 턴은 세션이
        스스로 이어간다."""
        nudge_text = (nudge_text or "").strip()
        if not nudge_text:
            return user_text
        return f"[방금 내가 사용자에게 먼저 건넨 말] {nudge_text}\n\n{user_text}"

    def _on_nudge_reply_submit(self, text: str) -> None:
        """자율발화를 보고 연 입력창에서 제출 — 첫 마디에만 nudge 문구를 얹는다."""
        nudge_text, self._pending_nudge_text = getattr(self, "_pending_nudge_text", ""), ""
        self._on_bubble_submit(self._with_nudge_context(nudge_text, text))

    def _on_nudge_input_closed(self) -> None:
        """자율발화를 보고 연 입력창을 아무것도 안 보내고 닫았다 — 중간 신호.
        완전한 무시보다는 약한 부정 신호로 기록된다."""
        self._pending_nudge_text = ""
        if not self._nudge_awaiting_reply:
            return
        self._nudge_awaiting_reply = False
        # 살아있는 발화만 "열어보고 무응답"으로 판정한다. 지난 발화를 되살려 봤다가
        # 그냥 닫은 경우는 이미 결과가 확정된 건이라 다시 집계하지 않는다.
        if self._nudge_engage_live:
            self._initiative.notify_acknowledged()

    def _on_initiative_outcome(self, nudge, outcome: str, shown_text: str, latency_sec: float = 0.0) -> None:
        """자율발화 하나의 결과가 확정됐다 — 여기가 "발화하고 끝"을 끊는 지점이다.

        엔진은 백오프까지만 알고, 무엇을 남길지는 전부 여기서 결정한다:
        활동 로그(집계 원장) · 참여한 호기심 해소 · 반복 무시 기록.
        어느 하나가 실패해도 나머지는 진행한다 — 부가 기록 때문에 UI 흐름이 끊기면 안 된다."""
        from overlay.bubble.initiative import ENGAGED, IGNORED, LATE_ENGAGED

        source = getattr(nudge, "source_key", "?")
        topic = getattr(nudge, "topic", "") or ""
        try:
            from core.observability.activity import log_activity

            # 형식 계약: 첫 " | " 앞은 기계 판독용 key=value 구간이고, 값에는 공백이
            # 들어가지 않는 것들만 둔다(source/outcome 은 고정 enum, latency 는 숫자).
            # topic·문구는 자유 텍스트라 구분자가 섞일 수 있으므로 반드시 뒤로 뺀다 —
            # 앞 구간에 섞으면 나중에 집계할 때 파싱이 조용히 깨진다. 뒤에 필드를
            # 추가해도 앞 구간 파서는 그대로 동작한다.
            detail = f"source={source} outcome={outcome} latency={latency_sec:.1f}"
            tail = " / ".join(p for p in (topic, shown_text[:200] if shown_text else "") if p)
            if tail:
                detail += f" | {tail}"
            log_activity(
                action=f"initiative.{outcome}",
                detail=detail,
                project="overlay",
                actor="engram-overlay",
            )
        except Exception:
            log.debug("[overlay] 자율발화 결과 로깅 실패", exc_info=True)

        # 참여한 호기심은 해소 처리 — 안 하면 이미 같이 파본 주제로 계속 다시 말을 건다.
        ref_id = getattr(nudge, "ref_id", None)
        if outcome in (ENGAGED, LATE_ENGAGED) and source == "curiosity" and ref_id is not None:
            try:
                from core.identity import address_curiosity

                address_curiosity(int(ref_id))
                log.info("[overlay] 자율발화 참여로 호기심 해소 id=%s", ref_id)
            except Exception:
                log.debug("[overlay] 호기심 해소 실패 id=%s", ref_id, exc_info=True)

        log.info("[overlay] 자율발화 결과 source=%s outcome=%s latency=%.1fs topic=%s",
                 source, outcome, latency_sec, topic)
        if outcome == IGNORED:
            self._note_ignored_source(source)
        elif outcome == LATE_ENGAGED:
            # 결국 응했으므로 "관심 낮음" 누적을 되돌린다 — 그 소재는 쓸모가 있었다.
            self._ignored_streaks.pop(source, None)

    def _note_ignored_source(self, source: str) -> None:
        """같은 소재를 연속으로 무시하면 "관심 낮음"을 약한 신호로 남긴다.

        한 번의 무시는 그냥 바빴던 것일 수 있어서 아무 의미가 없다 — 연속 3회부터
        신호로 친다. 카운터는 세션 수명만큼만 유지한다(영속화까지 할 만한 확신이 없음)."""
        streaks = getattr(self, "_ignored_streaks", None)
        if streaks is None:
            streaks = self._ignored_streaks = {}
        streaks[source] = streaks.get(source, 0) + 1
        if streaks[source] < 3:
            return
        streaks[source] = 0
        try:
            from core.observability.activity import log_activity

            log_activity(
                action="initiative.low_interest",
                detail=f"source={source} — 연속 3회 무시",
                project="overlay",
                actor="engram-overlay",
            )
        except Exception:
            log.debug("[overlay] 관심 낮음 신호 기록 실패", exc_info=True)

    def show_bubble_history(self) -> None:
        self._bubble_history.show()

    def new_bubble_session(self) -> None:
        """말풍선 상주 세션을 리셋한다(= 새 대화 시작). overlay 재시작 불필요.

        STM HTTP 스레드(/bubble/new)나 트레이 메뉴 등 다른 스레드에서 호출될 수 있어
        tk 메인 스레드로 마샬링한다. 현재 턴 응답이 마무리될 여지를 주려고 약간 지연 후
        세션을 stop 하고 resume 대상(claude_session_id)을 비운다. 다음 입력부터
        _ensure_bubble_session 이 resume=None 으로 새 세션을 지연 기동한다.
        """
        def _reset() -> None:
            try:
                set_bubble_session_id(None)
                if self._bubble_session is not None:
                    self._bubble_session.stop()
                    self._bubble_session = None
                log.info("[overlay] 말풍선 새 세션 — session_id 리셋 완료")
            except Exception:
                log.exception("[overlay] 말풍선 새 세션 리셋 실패")

        try:
            self.root.after(300, _reset)
        except Exception:
            _reset()

    def _on_shutdown_request(self):
        """/shutdown HTTP 요청으로 트리거되는 graceful shutdown."""
        self._quit_reason = "stm_shutdown_request"
        log.info("[overlay] /shutdown 요청 수신 — graceful 종료 시작")
        self.request_quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self._overlay_events.stop()
        try:
            self.character.cancel_config_watch()
        except Exception:
            pass
        log.info("[overlay] quit 진입: reason=%s", self._quit_reason)

        if self._discord_bot:
            self._discord_bot.stop()
        try:
            self._initiative.stop()
        except Exception:
            pass
        if self._bubble_session is not None:
            self._bubble_session.stop(timeout=5.0)
            self._bubble_session = None
        self.chat.kill()
        self.tray.stop()
        keyboard.unhook_all()
        # STM → LTM 승격 (최대 15초 대기)
        try:
            from core.graph.semantic import maybe_promote_async

            t = maybe_promote_async(scope_key="overlay")
            t.join(timeout=15)
        except Exception as e:
            log.warning("STM promote failed at quit: %s", e)
        # 원격 터널 종료. 반드시 Popen 핸들로만 죽인다 — 이름(ssh.exe)으로 죽이면
        # 사용자가 따로 열어둔 SSH 세션까지 함께 끊긴다.
        try:
            self._tunnels.stop_all()
        except Exception:
            log.exception("[tunnel] 종료 실패")
        # MCP HTTP 서버 종료
        self._terminate_managed_process("_mcp_http_proc", "mcp_http", "MCP HTTP 서버")
        # dashboard 종료 (overlay가 직접 시작한 경우에만)
        self._terminate_managed_process("_dashboard_proc", "dashboard", "dashboard")
        # kg_watcher 종료 (overlay가 직접 시작한 경우에만)
        self._terminate_managed_process("_kg_watcher_proc", "kg_watcher", "kg_watcher")
        self._stm_server.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    app = OverlayApp()
    app.run()


if __name__ == "__main__":
    main()
