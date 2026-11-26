"""core.observability.nonblocking_stdio: 막힌 로그 싱크가 호출자(asyncio 루프)를 막지 않는다."""

import asyncio
import io
import logging
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.observability import nonblocking_stdio as nbs


class _Sink(io.StringIO):
    """gate 가 열릴 때까지 write 가 막히는 가짜 원래 스트림. 쓰기 순서를 기록한다."""

    def __init__(self, gate=None, tag=""):
        super().__init__()
        self.gate = gate
        self.tag = tag
        self.log = None  # 공유 (tag, text) 리스트

    def write(self, text):
        if self.gate is not None:
            self.gate.wait(10)
        if self.log is not None:
            self.log.append((self.tag, text))
        return super().write(text)

    def fileno(self):
        return 42


@pytest.fixture
def streams(monkeypatch):
    """pytest 전역 캡처는 call 단계에서 sys.stdout 을 다시 덮으므로, 테스트 본문에서 호출해 교체한다."""
    gates = []

    def setup():
        nbs._uninstall_for_tests()
        gate = threading.Event()
        gates.append(gate)
        log = []
        out, err = _Sink(gate, "out"), _Sink(gate, "err")
        out.log = err.log = log
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        return out, err, gate, log

    yield setup
    for gate in gates:
        gate.set()
    nbs._uninstall_for_tests()


def _wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_blocked_sink_never_blocks_write_flush_or_logging(streams):
    out, err, gate, _ = streams()
    assert nbs.install() is True
    handler = logging.StreamHandler(sys.stderr)
    logger = logging.getLogger("nbs.test.blocked")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for action in (
            lambda: sys.stdout.write("hello\n"),
            lambda: sys.stderr.write("oops\n"),
            lambda: sys.stdout.flush(),
            lambda: sys.stderr.flush(),
            lambda: logger.info("through logging"),
        ):
            started = time.perf_counter()
            action()
            assert time.perf_counter() - started < 0.02
    finally:
        logger.removeHandler(handler)


def test_everything_is_written_in_order_after_unblock(streams):
    out, err, gate, log = streams()
    nbs.install()
    sys.stdout.write("a\n")
    sys.stderr.write("b\n")
    sys.stdout.write("c\n")
    sys.stderr.write("d\n")
    gate.set()
    assert _wait_for(lambda: len(log) == 4)
    assert log == [("out", "a\n"), ("err", "b\n"), ("out", "c\n"), ("err", "d\n")]
    assert out.getvalue() == "a\nc\n" and err.getvalue() == "b\nd\n"


def test_overflow_drops_and_reports_count(streams):
    out, err, gate, log = streams()
    nbs.install(max_queue_bytes=100)
    # 첫 항목은 기록 스레드가 꺼내 막힌 채 들고 있고, 나머지가 큐 한도(100)를 채운다.
    sys.stdout.write("x" * 10)
    assert _wait_for(lambda: nbs._core._in_flight)
    for _ in range(30):
        sys.stdout.write("y" * 10)
    gate.set()
    assert _wait_for(lambda: any("dropped" in text for _, text in log))
    notice = [text for _, text in log if "dropped" in text][0]
    assert notice.startswith("[engram-log] dropped ")
    assert notice.endswith(" writes while the log sink was blocked\n")
    dropped = int(notice.split()[2])
    written = sum(len(t) // 10 for _, t in log if "dropped" not in t)
    assert dropped > 0 and dropped + written == 31


def test_korean_utf8_is_preserved(streams):
    out, err, gate, log = streams()
    nbs.install()
    gate.set()
    sys.stderr.write("한글 로그 ✓\n")
    assert _wait_for(lambda: "한글 로그 ✓\n" in err.getvalue())
    assert sys.stderr.encoding == "utf-8" and sys.stderr.errors == "replace"


def test_install_is_idempotent_and_delegates_fileno(streams):
    out, err, gate, _ = streams()
    assert nbs.install() is True
    wrapper_out, wrapper_err = sys.stdout, sys.stderr
    assert nbs.install() is False
    assert sys.stdout is wrapper_out and sys.stderr is wrapper_err
    assert sum(t.name == "engram-log-writer" for t in threading.enumerate()) == 1
    assert wrapper_err.fileno() == 42 and wrapper_out.fileno() == 42
    assert wrapper_err.isatty() is False
    wrapper_err.reconfigure(encoding="utf-8")  # mcp_server.py 의 reconfigure 가 실패하지 않는다.


def test_rich_handler_is_replaced_by_single_line_plain_handler(streams):
    rich_logging = pytest.importorskip("rich.logging")
    from rich.console import Console

    out, err, gate, log = streams()
    gate.set()
    nbs.install()
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(rich_logging.RichHandler(console=Console(stderr=True)))
        root.setLevel(logging.INFO)
        assert nbs.replace_rich_handlers() == 1
        assert not any(type(h).__name__ == "RichHandler" for h in root.handlers)
        message = "[engram-call] tool=engram_report_claude_event lane=" + "z" * 220
        logging.getLogger("nbs.test.rich").info(message)
        assert _wait_for(lambda: "zzzz" in err.getvalue())
        lines = [line for line in err.getvalue().splitlines() if line]
        assert len(lines) == 1 and lines[0].endswith(message)
        assert "[INFO] nbs.test.rich:" in lines[0]
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])


def test_asyncio_loop_keeps_ticking_while_log_sink_is_blocked(streams):
    out, err, gate, _ = streams()
    nbs.install()
    logger = logging.getLogger("nbs.test.loop")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)

    async def scenario():
        worst = 0.0
        last = time.perf_counter()
        for i in range(200):
            logger.info("access log line %d", i)
            await asyncio.sleep(0)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now
        return worst

    try:
        worst = asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
    assert worst < 0.05
