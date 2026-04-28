"""오버레이 진입점 — 트레이 아이콘 + 전역 단축키 (Alt+F12) + 캐릭터 창."""

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

import keyboard
import pystray
from PIL import Image

from .character import CharacterOverlay
from .chat_window import ChatTerminal
from .config import (
    get_cli_provider,
    get_ollama_model,
    load_cfg,
    resolve_path,
    set_cli_provider,
    set_ollama_model,
)
from .settings_window import open_settings
from .stm_server import STMServer

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
    if not shutil.which("ollama"):
        with _ollama_cache_lock:
            _ollama_cache_ready = True
        return
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        models = []
        for i, line in enumerate(result.stdout.splitlines()):
            if i == 0:
                continue
            parts = line.strip().split()
            if parts:
                models.append(parts[0])
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

    def _build_claude_items():
        """Claude Code 서브메뉴 — 직접 vs Ollama 라우팅 선택."""
        items: list = [
            pystray.MenuItem(
                "claude (직접)",
                lambda: app._set_provider_model("claude-code", ""),
                checked=lambda _: app.get_cli_provider() == "claude-code" and not _is_ollama_routing_model(app._ollama_model),
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
        ]
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
                        lambda _, mod=model: app._set_provider_model("claude-code", mod),
                        checked=lambda _, mod=model: app.get_cli_provider() == "claude-code" and app._ollama_model == mod,
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
                    lambda _, mod=m: app._set_provider_model("ollama", mod),
                    checked=lambda _, mod=m: app.get_cli_provider() == "ollama" and app._ollama_model == mod,
                    radio=True,
                )
                for m in models
            ]
        items += [pystray.Menu.SEPARATOR, pystray.MenuItem("새로고침", lambda: _reload_ollama_models())]
        return items

    menu = pystray.Menu(
        pystray.MenuItem("채팅 열기/닫기", lambda: app.toggle_chat()),
        pystray.MenuItem(
            "CLI 공급자",
            pystray.Menu(
                pystray.MenuItem(
                    "Copilot CLI",
                    lambda: app._set_provider_model("copilot", None),
                    checked=lambda _: app.get_cli_provider() == "copilot",
                ),
                pystray.MenuItem(
                    "Gemini CLI",
                    lambda: app._set_provider_model("gemini", None),
                    checked=lambda _: app.get_cli_provider() == "gemini",
                ),
                pystray.MenuItem(
                    "Claude Code",
                    pystray.Menu(_build_claude_items),
                    checked=lambda _: app.get_cli_provider() == "claude-code",
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
        # Ollama 모델 목록 백그라운드 로드
        threading.Thread(target=_load_ollama_models, daemon=True).start()

        self.root = tk.Tk()
        self._app_icon = None
        try:
            icon_path = _resolve_icon_path()
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._app_icon)
        except Exception as e:
            log.warning("[overlay] app icon load failed: %s", e)
        self.root.withdraw()

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
        )

        self._mcp_http_proc = self._start_mcp_http_server()

        # MCP server가 KuzuDB write lock을 획득할 때까지 대기한 후
        # dashboard를 시작해야 cross-process lock 충돌이 없다.
        threading.Thread(target=self._deferred_startup, daemon=True).start()

        self._stm_server = STMServer(shutdown_callback=self._on_shutdown_request)
        self._stm_server.start()  # 포트 충돌 시 STMServer.start() 내부에서 조용히 실패

        keyboard.add_hotkey(hotkey, lambda: self.root.after(0, self._hotkey_chat))

        self.tray = _make_tray_icon(self)
        threading.Thread(target=self.tray.run, daemon=True).start()

        self._discord_bot = _try_start_discord_bot()

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._quitting = False

    def _start_mcp_http_server(self) -> "subprocess.Popen | None":
        """Copilot/Gemini CLI를 위한 지속 MCP HTTP(SSE) 서버를 overlay 수명에 맞춰 시작한다."""
        cfg = load_cfg()
        port = int((cfg.get("mcp") or {}).get("http_port", MCP_HTTP_PORT))
        # 이미 포트가 열려있으면 외부 MCP 서버 재사용 (dev_backend 등)
        try:
            _tc = __import__("socket").create_connection(("127.0.0.1", port), timeout=1)
            _tc.close()
            log.info("[mcp_http] 포트 %d 이미 응답 중 — 외부 MCP 서버 재사용, 시작 스킵", port)
            return None
        except OSError:
            pass  # 포트 비어있음 → 직접 시작
        py = _find_mcp_python()
        script = _find_mcp_script()
        if not py or not script:
            log.warning("[mcp_http] Python 또는 mcp_server.py를 찾을 수 없어 MCP HTTP 서버 시작 스킵")
            return None
        try:
            from core.runtime_config import get_db_root_dir

            env = os.environ.copy()
            env["ENGRAM_DB_DIR"] = get_db_root_dir()
            # overlay 역할 해제 — MCP 서버는 KuzuDB 직접 접근 가능
            env.pop("ENGRAM_RUNTIME_ROLE", None)
            log_path = Path.home() / ".engram" / "mcp-http.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            proc = subprocess.Popen(
                [py, str(script), "--transport", "sse", "--port", str(port)],
                env=env,
                cwd=str(script.parent),
                stdout=log_fh,
                stderr=log_fh,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log.info("[mcp_http] MCP HTTP 서버 시작 PID=%d port=%d", proc.pid, port)
            return proc
        except Exception as exc:
            log.warning("[mcp_http] MCP HTTP 서버 시작 실패: %s", exc)
            return None

    def _wait_mcp_ready(self, timeout: float = 15.0) -> bool:
        """MCP server가 /health 에 응답할 때까지 대기. 성공 시 True."""
        import time
        import socket

        cfg = load_cfg()
        port = int((cfg.get("mcp") or {}).get("http_port", MCP_HTTP_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=1)
                conn.close()
                log.info("[mcp_http] MCP server ready (port=%d)", port)
                return True
            except OSError:
                time.sleep(0.5)
        log.warning("[mcp_http] MCP server not ready after %.1fs", timeout)
        return False

    def _deferred_startup(self) -> None:
        """MCP server 준비 완료 후 kg_watcher / dashboard를 순서대로 시작한다."""
        self._wait_mcp_ready(timeout=15.0)
        self._kg_watcher_proc = self._start_kg_watcher()
        self._dashboard_proc = self._start_dashboard()

    def _start_dashboard(self) -> "subprocess.Popen | None":
        """engram_dashboard.py 를 streamlit으로 시작한다. 이미 실행 중이면 스킵."""
        import subprocess as _sp

        # 이미 실행 중인지 확인
        try:
            procs = _sp.run(
                [
                    "powershell",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*engram_dashboard*' } | Select-Object -First 1 -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            if procs.stdout.strip().isdigit():
                log.info("[dashboard] 이미 실행 중 (PID=%s) — 시작 스킵", procs.stdout.strip())
                return None
        except Exception:
            pass

        py = _find_mcp_python()
        if not py:
            log.warning("[dashboard] Python을 찾을 수 없어 시작 스킵")
            return None

        # streamlit 스크립트 경로
        script = (PROJECT_ROOT / "scripts" / "engram_dashboard.py").resolve()
        if getattr(sys, "frozen", False):
            streamlit_exe = Path(py).parent / "streamlit.exe"
        else:
            streamlit_exe = Path(py).parent / "streamlit"

        if not script.exists():
            log.warning("[dashboard] 스크립트 없음: %s", script)
            return None

        try:
            log_path = Path.home() / ".engram" / "dashboard.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            # streamlit CLI: python -m streamlit run ... or streamlit.exe run ...
            cmd: list
            if streamlit_exe.exists() or Path(str(streamlit_exe) + ".exe").exists():
                cmd = [str(streamlit_exe), "run", str(script), "--server.headless", "true", "--server.port", "8501"]
            else:
                cmd = [py, "-m", "streamlit", "run", str(script), "--server.headless", "true", "--server.port", "8501"]
            proc = _sp.Popen(
                cmd,
                cwd=str(script.parent.parent),
                stdout=log_fh,
                stderr=log_fh,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            log.info("[dashboard] 시작 PID=%d", proc.pid)
            return proc
        except Exception as exc:
            log.warning("[dashboard] 시작 실패: %s", exc)
            return None

    def _start_kg_watcher(self) -> "subprocess.Popen | None":
        """MCP server 준비 완료 후 kg_watcher를 overlay 자식 프로세스로 시작한다."""
        import subprocess as _sp

        py = _find_mcp_python()
        if not py:
            log.warning("[kg_watcher] Python을 찾을 수 없어 시작 스킵")
            return None

        script = (PROJECT_ROOT / "scripts" / "kg" / "kg_watcher.py").resolve()

        if not script.exists():
            log.warning("[kg_watcher] 스크립트 없음: %s", script)
            return None

        try:
            log_path = Path.home() / ".engram" / "kg-watcher.log"
            log_fh = open(str(log_path), "a", encoding="utf-8")
            proc = _sp.Popen(
                [py, str(script)],
                cwd=str(script.parent.parent.parent),
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
        log.info("[overlay] 재시작 요청")
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, "-m", "overlay.main"]
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
        open_settings(self.root, on_saved=self._reload_config)

    def _reload_config(self):
        """설정 저장 후 overlay config를 다시 읽어 반영한다."""
        cfg = load_cfg()
        new_provider = get_cli_provider(cfg)
        self._set_provider_model(new_provider, get_ollama_model(cfg))

    def get_cli_provider(self) -> str:
        return self._cli_provider

    def set_cli_provider(self, provider: str):
        """character overlay 우클릭 등 외부 콜백 호환용."""
        self._set_provider_model(provider, None)

    def _set_provider_model(self, provider: str, model: str | None):
        """provider와 ollama_model을 원자적으로 업데이트한다.
        model=None 이면 현재 모델 유지, model='' 이면 클리어.
        """
        normalized = set_cli_provider(provider)
        if model is not None:
            self._ollama_model = set_ollama_model(model)
        self._cli_provider = normalized
        self.chat.set_provider(normalized)
        try:
            self.tray.title = f"engram overlay ({normalized})"
        except Exception:
            pass
        log.info("[overlay] provider=%s model=%s", normalized, model)

    def toggle_chat(self):
        x, y, w, h = self.character.get_phys_rect()
        self.chat.show_at_overlay(x, y, w, h)

    def _hotkey_chat(self):
        self.chat.show_at_cursor()

    def _on_shutdown_request(self):
        """/shutdown HTTP 요청으로 트리거되는 graceful shutdown."""
        log.info("[overlay] /shutdown 요청 수신 — graceful 종료 시작")
        self.request_quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True

        if self._discord_bot:
            self._discord_bot.stop()
        self.chat.kill()
        self.tray.stop()
        keyboard.unhook_all()
        # STM → LTM 승격 (최대 15초 대기)
        try:
            from core.stm_promoter import maybe_promote_async

            t = maybe_promote_async(scope_key="overlay")
            t.join(timeout=15)
        except Exception as e:
            log.warning("STM promote failed at quit: %s", e)
        # MCP HTTP 서버 종료
        if self._mcp_http_proc and self._mcp_http_proc.poll() is None:
            self._mcp_http_proc.terminate()
            try:
                self._mcp_http_proc.wait(timeout=5)
            except Exception:
                self._mcp_http_proc.kill()
            log.info("[mcp_http] MCP HTTP 서버 종료")
        # dashboard 종료 (overlay가 직접 시작한 경우에만)
        if getattr(self, "_dashboard_proc", None) and self._dashboard_proc.poll() is None:
            self._dashboard_proc.terminate()
            try:
                self._dashboard_proc.wait(timeout=5)
            except Exception:
                self._dashboard_proc.kill()
            log.info("[dashboard] dashboard 종료")
        # kg_watcher 종료 (overlay가 직접 시작한 경우에만)
        if getattr(self, "_kg_watcher_proc", None) and self._kg_watcher_proc.poll() is None:
            self._kg_watcher_proc.terminate()
            try:
                self._kg_watcher_proc.wait(timeout=5)
            except Exception:
                self._kg_watcher_proc.kill()
            log.info("[kg_watcher] kg_watcher 종료")
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
