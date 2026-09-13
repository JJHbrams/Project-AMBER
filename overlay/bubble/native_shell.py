"""Bounded private stdio IPC. Callbacks run on worker threads, never on Tk."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading

PROTOCOL = 1
MAX_LINE_BYTES = 32 * 1024 * 1024
WINDOWS = frozenset({"input", "speech", "thought"})
ALLOWED_ACTIONS = frozenset({"submit", "edit", "save_edit", "cancel_edit", "delete",
    "interrupt", "send_now", "resume_queue", "history", "close", "input_activity",
    "resize_input", "presentation_size", "hover", "dismiss", "approval", "nudge_reply", "nudge_defer",
    "composer_state", "speech_history_state"})


@dataclass(eq=False)
class _Run:
    proc: object
    nonce: str = field(repr=False)
    outbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=8))
    ready: threading.Event = field(default_factory=threading.Event)
    stopping: threading.Event = field(default_factory=threading.Event)
    handshake: bool = False
    windows: set = field(default_factory=set)
    failed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class NativeBubbleShell:
    def __init__(self, on_action, on_crash, *, executable=None, extra_args=(), demo=False,
                 startup_timeout=10.0, on_ready=None, on_geometry=None):
        self._on_action, self._on_crash = on_action, on_crash
        self._on_ready = on_ready or (lambda: None)
        self._on_geometry = on_geometry or (lambda _: None)
        self._executable, self._extra_args = executable, tuple(extra_args)
        self._demo, self._startup_timeout = demo, startup_timeout
        self._run = None
        self._snapshot = {}

    @property
    def is_ready(self):
        r = self._run
        return bool(r and r.ready.is_set() and not r.stopping.is_set() and r.proc.poll() is None)

    @property
    def pid(self):
        r = self._run
        return r.proc.pid if r and r.proc.poll() is None else None

    def start(self, snapshot=None):
        if self.pid is not None:
            return False
        self._snapshot = dict(snapshot or {})
        nonce = secrets.token_urlsafe(32)
        env = {**os.environ, "ENGRAM_NATIVE_BUBBLE_PROTOCOL": str(PROTOCOL), "ENGRAM_NATIVE_BUBBLE_NONCE": nonce}
        try:
            proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            self._on_crash("spawn_failed")
            return False
        run = self._run = _Run(proc, nonce)
        for target in (self._read, self._write, self._watch, self._startup):
            threading.Thread(target=target, args=(run,), daemon=True, name="bubble-shell").start()
        return True

    def publish_snapshot(self, snapshot):
        self._snapshot = dict(snapshot)
        return self._enqueue({"type": "snapshot", "state": self._snapshot})

    def send_presentation(self, event):
        return self._enqueue({"type": "presentation", "event": dict(event)})

    def set_geometry(self, rect):
        return self._enqueue({"type": "geometry", "rect": dict(rect)})

    def stop(self, timeout=2.0):
        run = self._run
        if run is None:
            return True
        self._finish(run, timeout)
        if self._run is run:
            self._run = None
        return run.proc.poll() is not None

    def _command(self):
        if self._executable:
            path = Path(self._executable)
            result = [sys.executable, str(path)] if path.suffix.lower() == ".py" else [str(path)]
        else:
            name = "native-bubble-shell.exe" if os.name == "nt" else "native-bubble-shell"
            root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
            if getattr(sys, "frozen", False):
                path = root / "native-bubble-shell" / name
            else:
                candidates=[root / "native-bubble-shell" / "target" / mode / name for mode in ("release","debug")]
                existing=[candidate for candidate in candidates if candidate.is_file()]
                path=max(existing,key=lambda candidate:candidate.stat().st_mtime_ns) if existing else candidates[0]
            result = [str(path)]
        return result + list(self._extra_args) + (["--demo"] if self._demo else [])

    def _enqueue(self, message):
        run = self._run
        if not run or run.stopping.is_set() or run.proc.poll() is not None:
            return False
        raw = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_LINE_BYTES:
            return False
        try:
            run.outbox.put_nowait(raw)
            return True
        except queue.Full:
            self._fail(run, "transport_backpressure")
            return False

    def _fail(self, run, code):
        with run.lock:
            if run.failed or run.stopping.is_set() or run is not self._run:
                return
            run.failed = True
        try:
            self._on_crash(code)
        finally:
            threading.Thread(target=self._finish, args=(run, .5), daemon=True).start()

    def _finish(self, run, timeout):
        with run.lock:
            if run.stopping.is_set():
                return
            run.stopping.set()
            run.ready.clear()
        try:
            run.outbox.put_nowait(b'{"type":"shutdown"}\n')
        except queue.Full:
            pass
        try:
            run.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Exact owned Popen handle only, never a process-name kill.
            run.proc.terminate()
            try:
                run.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                run.proc.kill()
                run.proc.wait(timeout=2)
        finally:
            for stream in (run.proc.stdin, run.proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _write(self, run):
        while run.proc.poll() is None:
            try:
                raw = run.outbox.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                run.proc.stdin.write(raw)
                run.proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _read(self, run):
        try:
            while not run.stopping.is_set():
                raw = run.proc.stdout.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                    self._fail(run, "inbound_too_large")
                    return
                try:
                    msg = json.loads(raw)
                except (ValueError, UnicodeError):
                    self._fail(run, "invalid_json")
                    return
                self._handle(msg, run)
        except (OSError, ValueError):
            return

    def _handle(self, msg, run):
        if run is not self._run or run.stopping.is_set() or run.failed:
            return
        if not isinstance(msg, dict):
            self._fail(run, "invalid_envelope")
            return
        kind = msg.get("type")
        if not run.handshake:
            if (kind != "hello" or msg.get("protocol") != PROTOCOL
                or not secrets.compare_digest(str(msg.get("nonce", "")), run.nonce)):
                self._fail(run, "handshake_rejected")
                return
            run.handshake = True
            return
        if kind == "ready" and msg.get("window") in WINDOWS:
            run.windows.add(msg["window"])
            if run.windows == WINDOWS and not run.ready.is_set():
                run.ready.set()
                self.publish_snapshot(self._snapshot)
                self._on_ready()
        elif kind == "geometry" and run.ready.is_set() and isinstance(msg.get("rect"), dict):
            self._on_geometry(msg["rect"])
        elif (kind == "action" and run.ready.is_set() and msg.get("action") in ALLOWED_ACTIONS
              and isinstance(msg.get("action_id"), str) and 0 < len(msg["action_id"]) <= 128
              and isinstance(msg.get("payload"), dict)):
            self._on_action({k: msg.get(k) for k in ("action_id", "request_id", "action", "payload")})

    def _watch(self, run):
        run.proc.wait()
        if not run.stopping.is_set():
            self._fail(run, "child_exited")

    def _startup(self, run):
        if not run.ready.wait(self._startup_timeout):
            self._fail(run, "startup_timeout")
