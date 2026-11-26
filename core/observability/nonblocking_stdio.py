"""sys.stdout/sys.stderr 쓰기가 호출자를 절대 막지 않게 하는 래퍼.

MCP 서버 자식 프로세스의 stdout/stderr 는 로그 파일 핸들에 직접 물려 있어, 디스크/AV/파이프가
막히면 logging 핸들러 안의 flush 가 asyncio 루프를 통째로 멈춘다. write() 는 제한 있는 큐에
넣고 즉시 반환하고, 데몬 스레드 하나가 원래 스트림에 쓴다. 큐가 가득 차면 버리고 개수를 센다.
"""

from __future__ import annotations

import atexit
import collections
import logging
import sys
import threading
import time

DEFAULT_MAX_QUEUE_BYTES = 8 * 1024 * 1024
ATEXIT_DRAIN_SECS = 2.0
_THREAD_NAME = "engram-log-writer"

_state_lock = threading.Lock()
_core: "_Core | None" = None
_atexit_registered = False
_originals: tuple | None = None


class _Core:
    """하나의 큐와 하나의 기록 스레드. stdout/stderr 래퍼가 공유해 순서를 보존한다."""

    def __init__(self, max_queue_bytes: int, report_target) -> None:
        self.max_queue_bytes = max(1, int(max_queue_bytes))
        self._report_target = report_target
        self._cond = threading.Condition(threading.Lock())
        self._queue: collections.deque = collections.deque()
        self._queued_bytes = 0
        self._in_flight = False
        self._dropped = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, target, text: str) -> None:
        size = len(text)
        with self._cond:
            if self._queued_bytes + size > self.max_queue_bytes:
                self._dropped += 1
                return
            self._queue.append((target, text))
            self._queued_bytes += size
            self._cond.notify()

    def wake(self) -> None:
        # flush(): 기록 스레드만 깨우고 기다리지 않는다. 락을 못 잡아도 막지 않는다.
        if self._cond.acquire(blocking=False):
            try:
                self._cond.notify()
            finally:
                self._cond.release()

    def _run(self) -> None:
        touched: list = []
        while True:
            try:
                with self._cond:
                    while not self._queue and not self._stop and not touched and not self._dropped:
                        self._cond.wait()
                    if self._stop and not self._queue:
                        return
                    item = self._queue.popleft() if self._queue else None
                    if item is not None:
                        self._queued_bytes -= len(item[1])
                        self._in_flight = True
                if item is None:
                    # 큐가 비었다: 쓴 스트림을 한 번 flush 하고 버림 보고를 남긴다.
                    self._flush_touched(touched)
                    touched = []
                    self._maybe_report_drops()
                    with self._cond:
                        self._cond.notify_all()
                    continue
                target, text = item
                try:
                    target.write(text)
                    if target not in touched:
                        touched.append(target)
                except Exception:
                    with self._cond:
                        self._dropped += 1
                finally:
                    with self._cond:
                        self._in_flight = False
                        self._cond.notify_all()
            except Exception:
                time.sleep(0.05)

    @staticmethod
    def _flush_touched(touched: list) -> None:
        for target in touched:
            try:
                target.flush()
            except Exception:
                pass

    def _maybe_report_drops(self) -> None:
        with self._cond:
            count = self._dropped
        if not count or self._report_target is None:
            return
        try:
            self._report_target.write(
                f"[engram-log] dropped {count} writes while the log sink was blocked\n"
            )
            self._report_target.flush()
        except Exception:
            time.sleep(0.05)
            return
        with self._cond:
            self._dropped -= count

    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            self._cond.notify()
            while self._queue or self._in_flight or self._dropped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(min(remaining, 0.05))


class _NonBlockingStream:
    """TextIO 호환 래퍼. 모든 쓰기는 큐로 가고 즉시 반환한다."""

    encoding = "utf-8"
    errors = "replace"
    newlines = None

    def __init__(self, core: _Core, original) -> None:
        self._core = core
        self._original = original
        self.closed = False

    def write(self, text) -> int:
        if not isinstance(text, str):
            try:
                if isinstance(text, (bytes, bytearray)):
                    text = bytes(text).decode("utf-8", "replace")
                else:
                    text = str(text)
            except Exception:
                return 0
        if text:
            self._core.enqueue(self._original, text)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._core.wake()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        # faulthandler 등 fd 를 직접 쓰는 곳은 원래 스트림의 fd 를 계속 쓴다.
        return self._original.fileno()

    def reconfigure(self, **_kwargs) -> None:
        # 교체 뒤 mcp_server 의 reconfigure 호출이 실패하지 않게 한다. 인코딩은 이미 utf-8/replace.
        return None

    def close(self) -> None:
        # 프로세스 전역 스트림을 닫지 않는다.
        return None

    def __getattr__(self, name):
        # buffer, name, mode 등은 원래 스트림에 위임한다.
        return getattr(self._original, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _atexit_drain() -> None:
    core = _core
    if core is not None:
        try:
            core.drain(ATEXIT_DRAIN_SECS)
        except Exception:
            pass


def install(max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES) -> bool:
    """sys.stdout/sys.stderr 를 비차단 래퍼로 교체한다. 멱등. 적용했으면 True."""
    global _core, _atexit_registered, _originals
    with _state_lock:
        if _core is not None:
            return False
        out, err = sys.stdout, sys.stderr
        if out is None or err is None:
            return False
        if isinstance(out, _NonBlockingStream) or isinstance(err, _NonBlockingStream):
            return False
        core = _Core(max_queue_bytes, err)
        _originals = (out, err)
        core.start()
        _core = core
        sys.stdout = _NonBlockingStream(core, out)
        sys.stderr = _NonBlockingStream(core, err)
        if not _atexit_registered:
            atexit.register(_atexit_drain)
            _atexit_registered = True
        return True


def _uninstall_for_tests() -> None:
    """테스트 전용: 원래 스트림을 복원하고 기록 스레드를 멈춘다."""
    global _core, _originals
    with _state_lock:
        core, originals = _core, _originals
        _core, _originals = None, None
        if originals is not None:
            sys.stdout, sys.stderr = originals
        if core is not None:
            with core._cond:
                core._stop = True
                core._cond.notify_all()


PLAIN_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def replace_rich_handlers(root: logging.Logger | None = None) -> int:
    """루트 로거의 RichHandler 를 줄바꿈 없는 일반 StreamHandler(sys.stderr)로 바꾼다. 교체 수 반환.

    RichHandler 는 비터미널에서 80칸으로 줄을 강제로 꺾어 한 줄 기록이 여러 줄로 쪼개진다(#16).
    레벨은 기존 핸들러를 그대로 이어받는다.
    """
    root = root or logging.getLogger()
    replaced = 0
    for handler in list(root.handlers):
        if type(handler).__name__ != "RichHandler":
            continue
        plain = logging.StreamHandler(sys.stderr)
        plain.setLevel(handler.level)
        plain.setFormatter(logging.Formatter(PLAIN_LOG_FORMAT))
        root.removeHandler(handler)
        root.addHandler(plain)
        replaced += 1
    return replaced
