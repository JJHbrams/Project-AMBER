"""기동 추적: 단계 표식 형식과 faulthandler 스택 덤프 무장/해제."""

import io
import os
import re
import sys
import time

from core.observability import startup_trace


class _FlushRecorder(io.StringIO):
    flushed = 0

    def flush(self):
        self.flushed += 1
        super().flush()


def test_mark_writes_one_formatted_line_and_flushes(monkeypatch):
    stream = _FlushRecorder()
    monkeypatch.setattr(sys, "stderr", stream)
    startup_trace.mark("db_init")
    line = stream.getvalue()
    assert re.fullmatch(rf"\[engram-startup\] pid={os.getpid()} stage=db_init t=\+\d+\.\d+s\n", line)
    assert stream.flushed >= 1


def test_mark_survives_missing_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    startup_trace.mark("entry")  # 예외 없이 무시


def test_armed_dump_writes_traceback_to_target(tmp_path):
    target = open(tmp_path / "dump.log", "w", encoding="utf-8")
    try:
        startup_trace.arm(0.3, target=target)
        time.sleep(1.0)
    finally:
        startup_trace.disarm()
        target.close()
    text = (tmp_path / "dump.log").read_text(encoding="utf-8")
    assert "most recent call first" in text


def test_disarm_cancels_the_dump(tmp_path):
    target = open(tmp_path / "dump.log", "w", encoding="utf-8")
    try:
        startup_trace.arm(0.5, target=target)
        startup_trace.disarm()
        time.sleep(1.0)
    finally:
        startup_trace.disarm()
        target.close()
    assert (tmp_path / "dump.log").read_text(encoding="utf-8") == ""


def test_dump_target_falls_back_to_log_file_when_stderr_is_unusable(monkeypatch, engram_home):
    devnull = open(os.devnull, "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", devnull)
    try:
        handle = startup_trace._dump_target()
        try:
            assert os.path.basename(handle.name) == "mcp-http.log"
            assert handle.name.startswith(str(engram_home))
        finally:
            handle.close()
    finally:
        devnull.close()


def test_dump_target_uses_stderr_when_it_has_a_real_fd(monkeypatch, tmp_path):
    real = open(tmp_path / "err.log", "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", real)
    try:
        assert startup_trace._dump_target() is real
    finally:
        real.close()


def test_mark_falls_back_to_log_file_when_stderr_is_unusable(monkeypatch, engram_home):
    devnull = open(os.devnull, "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", devnull)
    try:
        startup_trace.mark("entry")
    finally:
        devnull.close()
    text = (engram_home / ".engram" / "mcp-http.log").read_text(encoding="utf-8")
    assert re.search(r"\[engram-startup\] pid=\d+ stage=entry t=\+", text)


def test_disarm_closes_the_fallback_dump_handle(monkeypatch, engram_home):
    devnull = open(os.devnull, "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", devnull)
    try:
        startup_trace.arm(60)
        handle = startup_trace._dump_handle
        assert handle is not None and not handle.closed
        startup_trace.disarm()
        assert handle.closed and startup_trace._dump_handle is None
    finally:
        startup_trace.disarm()
        devnull.close()


def test_stdio_transport_disarms_the_startup_dump(tmp_path):
    from unittest.mock import patch

    import mcp_server

    target = open(tmp_path / "dump.log", "w", encoding="utf-8")
    try:
        startup_trace.arm(60, target=target)
        assert startup_trace._armed
        with patch.object(mcp_server.engramMCP, "run") as run:
            mcp_server.main(["--transport", "stdio"])
        run.assert_called_once()
        assert not startup_trace._armed
    finally:
        startup_trace.disarm()
        target.close()
