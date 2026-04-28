"""MCP 도구 호출 로그 + crash report."""
from __future__ import annotations

import atexit
import json
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


_LOG_DIR: Path = Path.home() / ".engram" / "logs"


def _get_log_dir() -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


class _CallLog:
    def __init__(self, maxlen: int = 100) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, tool_name: str, kwargs: dict[str, Any]) -> None:
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "tool": tool_name,
            "kwargs": _truncate(kwargs, max_len=200),
        }
        with self._lock:
            self._buf.append(entry)

    def dump_crash_report(self, exc: BaseException | None = None) -> Path | None:
        try:
            log_dir = _get_log_dir()
            fname = log_dir / f"crash-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.log"
            lines = [f"=== CRASH REPORT {datetime.utcnow().isoformat()} ===\n"]
            if exc is not None:
                lines.append("EXCEPTION:\n")
                lines.extend(traceback.format_exception(type(exc), exc, exc.__traceback__))
                lines.append("\n")
            lines.append(f"LAST {len(self._buf)} TOOL CALLS:\n")
            with self._lock:
                buf_copy = list(self._buf)
            for entry in buf_copy:
                lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            fname.write_text("".join(lines), encoding="utf-8")
            return fname
        except Exception:
            return None

    def dump_session_log(self) -> Path | None:
        """정상 종료 시 세션 로그 저장."""
        try:
            log_dir = _get_log_dir()
            fname = log_dir / f"session-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.log"
            with self._lock:
                buf_copy = list(self._buf)
            lines = [f"=== SESSION LOG {datetime.utcnow().isoformat()} ===\n"]
            for entry in buf_copy:
                lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            fname.write_text("".join(lines), encoding="utf-8")
            return fname
        except Exception:
            return None


def _truncate(obj: Any, max_len: int = 200) -> Any:
    """kwargs를 로그용으로 truncate."""
    if isinstance(obj, dict):
        return {k: _truncate(v, max_len) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > max_len:
        return obj[:max_len] + "...[truncated]"
    return obj


# 싱글톤
call_log = _CallLog(maxlen=100)


def _atexit_handler() -> None:
    call_log.dump_session_log()


atexit.register(_atexit_handler)


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    call_log.dump_crash_report(exc_value)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args) -> None:
    call_log.dump_crash_report(args.exc_value)


sys.excepthook = _excepthook
threading.excepthook = _threading_excepthook
