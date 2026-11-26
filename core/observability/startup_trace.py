"""MCP 서버 기동 단계 추적.

기동이 멈췄을 때 어디서 멈췄는지 남긴다. 단계 표식은 stderr 로 쓰고, 일정 시간 안에
uvicorn 이 뜨지 않으면 faulthandler 가 모든 스레드의 스택(프레임 위치만)을 덤프한다.
인자, 경로 내용, 환경 값은 기록하지 않는다.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
from pathlib import Path

_T0 = time.perf_counter()
_dump_handle = None
_armed = False


def _stderr_usable(need_fd: bool = False) -> bool:
    """mark 는 쓸 수 있는 stream 이면 되고, faulthandler 덤프는 실제 fd 까지 필요하다."""
    stream = sys.stderr
    try:
        if stream is not None and getattr(stream, "name", None) != os.devnull:
            if need_fd:
                stream.fileno()
            return True
    except Exception:
        pass
    return False


def _fallback_path() -> Path:
    return Path.home() / ".engram" / "mcp-http.log"


def mark(stage: str) -> None:
    """한 줄 쓰고 flush 한다: [engram-startup] pid=N stage=NAME t=+SEC (stderr 가 못 쓰면 로그 파일)"""
    line = f"[engram-startup] pid={os.getpid()} stage={stage} t=+{time.perf_counter() - _T0:.2f}s\n"
    try:
        if _stderr_usable():
            sys.stderr.write(line)
            sys.stderr.flush()
            return
        path = _fallback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        pass


def _dump_target():
    """faulthandler 는 실제 fd 가 있는 파일이 필요하다. stderr 가 없거나 devnull 이면 로그 파일로 돌린다."""
    global _dump_handle
    if _stderr_usable(need_fd=True):
        return sys.stderr
    path = _fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_handle = open(path, "a", encoding="utf-8")
    return _dump_handle


def arm(timeout_secs: float | None = None, target=None) -> None:
    """timeout 안에 disarm() 되지 않으면 전 스레드 스택을 한 번 덤프한다."""
    global _armed
    if _armed:
        return
    try:
        if timeout_secs is None:
            timeout_secs = float(os.environ.get("ENGRAM_STARTUP_STACKDUMP_SECS", 90))
        faulthandler.dump_traceback_later(timeout_secs, repeat=False, file=target or _dump_target())
        _armed = True
    except Exception:
        pass


def disarm() -> None:
    global _armed, _dump_handle
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    _armed = False
    if _dump_handle is not None:
        try:
            _dump_handle.close()
        except Exception:
            pass
        _dump_handle = None
