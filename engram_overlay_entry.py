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


def _shutdown_existing_overlay() -> None:
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
                return
    except Exception:
        _raw_log("[entry] 기존 overlay 없음 — 바로 시작")
        return

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
            return

    # 4) 타임아웃 — PID로 강제 종료 (최후 수단)
    if old_pid:
        _raw_log(f"[entry] 타임아웃 — PID {old_pid} 강제 종료")
        try:

            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, old_pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
                _raw_log(f"[entry] PID {old_pid} 강제 종료 완료")
        except Exception as e:
            _raw_log(f"[entry] 강제 종료 실패: {e}")


def _kill_orphan_engram_children() -> None:
    """이전 overlay 크래시 등으로 고아가 된 engram 자식 프로세스(python.exe)를 정리한다.

    mcp_server.py / engram_dashboard.py / kg_watcher.py 를 실행 중인
    python.exe 프로세스를 WMI 명령줄 검색으로 찾아 종료한다.
    """
    import subprocess as _sp

    patterns = ["mcp_server.py", "engram_dashboard.py", "kg_watcher.py"]
    for pattern in patterns:
        try:
            result = _sp.run(
                [
                    "powershell", "-Command",
                    f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                    f" | Where-Object {{ $_.CommandLine -like '*{pattern}*' }}"
                    f" | Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    try:
                        PROCESS_TERMINATE = 0x0001
                        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                        if handle:
                            ctypes.windll.kernel32.TerminateProcess(handle, 0)
                            ctypes.windll.kernel32.CloseHandle(handle)
                            _raw_log(f"[entry] 고아 프로세스 종료: {pattern} PID={pid}")
                    except Exception as e:
                        _raw_log(f"[entry] 고아 종료 실패 PID={pid}: {e}")
        except Exception as e:
            _raw_log(f"[entry] 고아 탐색 실패 ({pattern}): {e}")


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

    _raw_log("[entry] 기존 overlay 종료 처리 시작")
    _shutdown_existing_overlay()
    _raw_log("[entry] 고아 자식 프로세스 정리")
    _kill_orphan_engram_children()
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
    pass  # 위 try 블록에서 이미 main() 호출됨
