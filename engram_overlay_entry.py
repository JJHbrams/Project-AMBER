"""engram-overlay.exe 빌드용 엔트리포인트."""

import os
import sys
from pathlib import Path as _Path
import time
import datetime
import logging
import traceback
import yaml
import json as _json
import urllib.request
import urllib.error
import ctypes

from core.install.versioning import resolve_version


def _prepare_frozen_streams() -> None:
    """Give console-oriented libraries valid streams in a windowed executable."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            mode = "r" if name == "stdin" else "w"
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def _write_frozen_failure(label: str) -> None:
    path = os.environ.get("ENGRAM_SMOKE_LOG")
    if not path:
        path = str(_Path.home() / ".engram" / "logs" / "frozen-smoke.log")
    try:
        log_path = _Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.datetime.now().isoformat()}] {label}\n")
            traceback.print_exc(file=handle)
    except OSError:
        pass


def _run_dashboard_sidecar(argv: list[str]) -> None:
    """Run the dedicated frozen Streamlit dashboard sidecar."""
    import argparse

    parser = argparse.ArgumentParser(description="Engram dashboard sidecar")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--smoke-check", action="store_true")
    args = parser.parse_args(argv)

    app_path = _Path(getattr(sys, "_MEIPASS", _Path(__file__).parent)) / "core" / "dashboard" / "app.py"
    if not app_path.is_file():
        raise RuntimeError(f"bundled dashboard entry missing: {app_path}")

    if args.smoke_check:
        from streamlit.testing.v1 import AppTest

        result = AppTest.from_file(str(app_path), default_timeout=30).run()
        if result.exception:
            messages = "; ".join(str(item.value) for item in result.exception)
            raise RuntimeError(f"dashboard render smoke failed: {messages}")
        titles = [str(item.value) for item in result.title]
        if not any("Overview" in title for title in titles):
            raise RuntimeError(f"dashboard title missing: {titles}")
        return

    from streamlit.web import bootstrap as streamlit_bootstrap

    options = {
        "server.headless": True,
        "server.port": max(1, min(65535, int(args.port))),
        "global.developmentMode": False,
        "browser.gatherUsageStats": False,
    }
    streamlit_bootstrap.load_config_options(options)
    streamlit_bootstrap.run(
        str(app_path),
        False,
        [],
        options,
    )


# ── 멀티콜 바이너리 디스패치 ────────────────────────────────────────────
# 같은 exe 가 `--role` 인자에 따라 백엔드(mcp_server / kg_watcher)로도 동작한다.
# frozen 번들에서 conda python 없이 백엔드를 구동하기 위함(통짜 installer 핵심).
# 백엔드 역할이면 무거운 tk/pystray(overlay.main) import 전에 바로 처리하고 종료한다.
# overlay 역할일 때만 아래로 계속 진행한다.
def _dispatch_backend_role() -> bool:
    argv = sys.argv[1:]
    if getattr(sys, "frozen", False):
        _prepare_frozen_streams()
        if _Path(sys.executable).stem.lower() == "engram-dashboard":
            try:
                _run_dashboard_sidecar(argv)
                return True
            except BaseException:
                _write_frozen_failure("dashboard sidecar")
                sys.exit(1)
    if not argv or argv[0] != "--role":
        return False
    role = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    # 백엔드 역할: UTF-8 콘솔 강제. frozen exe 는 stdout 이 cp949(한국어 로케일)로 잡혀
    # kg_watcher/mcp_server 의 한글·이모지 로그 줄에서 UnicodeEncodeError 로 크래시한다.
    _prepare_frozen_streams()
    if not getattr(sys, "frozen", False):
        # 소스 모드: 루트 및 scripts/kg 를 import 경로에 추가
        here = _Path(__file__).parent
        sys.path.insert(0, str(here))
        sys.path.insert(0, str(here / "scripts" / "kg"))
    # 백엔드 역할은 부모(overlay)가 stdout/stderr 를 로그 파일로 리다이렉트한 서브프로세스다.
    # 크래시 시 PyInstaller 윈도우 부트로더의 모달 다이얼로그가 뜨지 않도록 여기서 잡아
    # stderr(=로그)로 트레이스백만 남기고 조용히 종료한다.
    try:
        if role == "mcp-server":
            import mcp_server
            mcp_server.main(rest)
            return True
        if role == "claude-root-launcher":
            from core.integrations.claude_root_launcher import main
            raise SystemExit(main(argv[2:]))
        if role == "kg-watcher":
            import kg_watcher
            kg_watcher.main(rest)
            return True
        if role == "install-bootstrap":
            from core.install.bootstrap import main as bootstrap_main
            bootstrap_main(rest)
            return True
        if role == "policy-preflight":
            from core.integrations.policy_preflight import main as policy_preflight_main

            policy_preflight_main(rest)
            return True
        if role == "agent-policy-hook":
            from core.integrations.agent_policy_hook import main as agent_policy_hook_main

            raise SystemExit(agent_policy_hook_main(rest))
        if role == "git-hook":
            from core.integrations.git_policy_hook import main as git_hook_main

            git_hook_main(rest)
            return True
        if role == "install-user-config":
            from core.install.user_config import main as user_config_main
            user_config_main(rest)
            return True
        if role == "runtime-contract":
            from core.install.runtime_contract import main as runtime_contract_main

            raise SystemExit(runtime_contract_main(rest))
        if role == "smoke-check":
            from mcp.server.fastmcp import FastMCP

            if not callable(FastMCP):
                raise RuntimeError("mcp.server.fastmcp.FastMCP is not importable")
            import mcp_server
            import kg_watcher
            import overlay.main

            from core.graph.semantic import get_semantic_graph, run_sg_coro

            graph = get_semantic_graph()
            vector = run_sg_coro(
                graph.compute_query_embedding("engram smoke check")
            )
            if len(vector) != 384:
                raise RuntimeError(
                    "embedding smoke check failed: "
                    f"model={graph.embedding_model_name}, dimension={len(vector)}"
                )
            return True
        if role == "embedding-check":
            from core.graph.semantic import get_semantic_graph, run_sg_coro
            graph = get_semantic_graph()
            vector = run_sg_coro(
                graph.compute_query_embedding("engram embedding check")
            )
            if len(vector) != 384:
                raise RuntimeError(
                    "bundled embedding model validation failed: "
                    f"model={graph.embedding_model_name}, dimension={len(vector)}"
                )
            return True
        raise SystemExit(f"[entry] unknown --role: {role!r}")
    except SystemExit:
        raise
    except BaseException:
        _write_frozen_failure(f"backend role {role}")
        sys.exit(1)


if _dispatch_backend_role():
    sys.exit(0)

from overlay.main import main

# ── 가장 먼저: import 전에도 파일에 기록하는 원시 로거 ───────────────────
_log_path = _Path.home() / ".engram" / "overlay.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)

# 세션 로그 파일: ~/.engram/logs/overlay-YYYYMMDD-HHMMSS.log
_session_ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
_session_log_dir = _Path.home() / ".engram" / "logs"
_session_log_dir.mkdir(parents=True, exist_ok=True)
_session_log_path = _session_log_dir / f"overlay-{_session_ts}.log"


def _raw_log(msg: str) -> None:
    """logging 모듈 없이 타임스탬프와 함께 overlay.log + 세션 로그에 한 줄 추가."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {msg}\n"
        with open(str(_log_path), "a", encoding="utf-8") as _f:
            _f.write(line)
        with open(str(_session_log_path), "a", encoding="utf-8") as _f:
            _f.write(line)
    except Exception:
        pass


_raw_log(f"[entry] 시작 — frozen={getattr(sys, 'frozen', False)}" f", cwd={os.getcwd()}" f", exe={sys.executable}")

# KuzuDB 소유권: overlay 프로세스는 KuzuDB를 열지 않음 (MCP 서버 독점).
# 반드시 다른 import보다 먼저 설정해야 함.
os.environ["ENGRAM_RUNTIME_ROLE"] = "overlay"

# ── 기존 overlay 프로세스 graceful shutdown ────────────────────────────────


def _get_stm_port() -> int:
    try:

        cfg_path = _Path.home() / ".engram" / "user.config.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            return int(cfg.get("overlay", {}).get("stm_server_port", 17384))
    except Exception:
        pass
    return 17384


def _shutdown_existing_overlay() -> bool:
    """기존 overlay 인스턴스에 graceful shutdown을 요청하고 종료를 기다린다."""

    port = _get_stm_port()
    base = f"http://127.0.0.1:{port}"

    # 1) 헬스 체크 — 기존 인스턴스가 있는지 확인
    # role='overlay-stm' 인 경우만 실제 overlay 인스턴스로 판단 (dev_backend STM 브로커와 구분)
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:

            info = _json.loads(resp.read().decode())
            old_pid = info.get("pid")
            if info.get("role") != "overlay-stm":
                _raw_log(f"[entry] /health 응답이 overlay-stm 이 아님 (role={info.get('role')}) — 외부 STM 브로커로 판단, 바로 시작")
                return True
    except Exception:
        _raw_log("[entry] 기존 overlay 없음 — 바로 시작")
        return True

    _raw_log(f"[entry] 기존 overlay 발견 (PID={old_pid}) — graceful shutdown 요청")

    # 2) /shutdown POST
    try:
        req = urllib.request.Request(
            f"{base}/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        _raw_log(f"[entry] /shutdown 요청 실패 (무시): {e}")

    # 3) 종료 대기 (최대 15초)
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"{base}/health", timeout=1)
        except Exception:
            _raw_log("[entry] 기존 overlay 종료 확인됨")
            return True

    # 4) 타임아웃 — PID로 강제 종료 (최후 수단)
    if old_pid:
        _raw_log(f"[entry] 타임아웃 — PID {old_pid} 강제 종료")
        try:

            PROCESS_TERMINATE = 0x0001
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, old_pid)
            if handle:
                terminated = bool(ctypes.windll.kernel32.TerminateProcess(handle, 0))
                wait_result = ctypes.windll.kernel32.WaitForSingleObject(handle, 5000) if terminated else 0xFFFFFFFF
                ctypes.windll.kernel32.CloseHandle(handle)
                if wait_result == 0:
                    _raw_log(f"[entry] PID {old_pid} 강제 종료 확인 완료")
                    return True
        except Exception as e:
            _raw_log(f"[entry] 강제 종료 실패: {e}")
    return False


def _cleanup_dev_restart_orphans() -> None:
    if os.environ.get("ENGRAM_DEV_SOURCE_RESTART") != "1":
        return
    try:
        from core.install.process_identity import cleanup_dev_restart_orphans

        stopped = cleanup_dev_restart_orphans(_Path(__file__).resolve().parent)
        _raw_log(f"[entry] dev restart orphan cleanup stopped={stopped}")
    except Exception as exc:
        _raw_log(f"[entry] dev restart orphan cleanup failed: {exc}")


try:
    # 패키지 루트를 sys.path에 추가 (pyinstaller 환경 대응)
    sys.path.insert(0, str(_Path(__file__).parent))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(_log_path), encoding="utf-8"),
            logging.FileHandler(str(_session_log_path), encoding="utf-8"),
        ],
    )
    _raw_log("[entry] logging 설정 완료")
    _raw_log(f"[entry] Engram Overlay version={resolve_version().version}")

    _raw_log("[entry] 기존 overlay 종료 처리 시작")
    old_overlay_stopped = _shutdown_existing_overlay()
    if old_overlay_stopped:
        _raw_log("[entry] dev source restart의 allowlisted 고아 자식 정리")
        _cleanup_dev_restart_orphans()
    else:
        _raw_log("[entry] 기존 overlay 종료 미확인 — child cleanup 생략")
    _raw_log("[entry] overlay.main 임포트 완료, main() 호출")
    main()

except Exception as _e:

    _tb = traceback.format_exc()
    _raw_log(f"[entry] 치명적 오류: {_e}\n{_tb}")

    # --noconsole 환경에서도 오류를 알 수 있도록 메세지박스 표시
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"engram-overlay 시작 실패:\n\n{_e}\n\n로그: {_log_path}",
            "engram-overlay 오류",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    pass  # pyinstaller는 __main__ 블록을 실행하지 않음, main()은 위에서 바로 호출
