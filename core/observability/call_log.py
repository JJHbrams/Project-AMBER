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

    def audit_remote(
        self,
        *,
        principal: str,
        action: str,
        tool: str = "",
        path: str = "",
        detail: str = "",
    ) -> None:
        """원격 리스너를 통과한 요청을 즉시 디스크에 append 한다.

        인메모리 링버퍼(maxlen=100, 종료 시에만 flush)는 감사용으로 쓸 수 없다 —
        롤오버되고, 크래시가 아니면 남지 않는다. 원격 호출은 건별로 바로 적는다.
        토큰 값은 싣지 않는다. principal 은 이름만.
        """
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "principal": principal,
            "action": action,   # allow | deny | unauthorized
            "tool": tool,
            "path": path,
            "detail": detail,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with self._lock:
                with open(_get_log_dir() / "remote-audit.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
        except Exception:
            pass

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
