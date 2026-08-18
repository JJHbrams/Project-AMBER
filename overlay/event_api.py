"""Public, metadata-only Overlay Event API v1.

The transport is deliberately local: an optional renderer child receives JSONL on
stdin and writes its ``overlay.hello`` handshake to stdout.  Nothing listens on a
network port and conversation/tool content is never forwarded.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import queue
import subprocess
import threading
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1
DISPLAY_HINTS = frozenset({"default", "idle", "hover", "click", "input", "generating", "search", "thought", "memory", "success", "provider_error", "error"})


def tool_category(name: object) -> str:
    value = str(name or "").lower()
    if any(x in value for x in ("memory", "kg_", "recall")):
        return "memory"
    if any(x in value for x in ("search", "find", "web", "browser", "fetch", "grep", "glob", "list")):
        return "search"
    if any(x in value for x in ("read", "open")):
        return "read"
    if any(x in value for x in ("write", "edit", "patch", "delete")):
        return "write"
    if any(x in value for x in ("shell", "exec", "build", "test", "run")):
        return "execute"
    if any(x in value for x in ("mail", "message", "discord", "slack")):
        return "communication"
    return "other"


def event_for_bubble(event: object) -> tuple[str, str, dict[str, Any]] | None:
    """Convert an internal event without copying its text, paths, or tool payload."""
    if not isinstance(event, dict):
        return None
    kind = str(event.get("kind") or "").lower()
    if kind == "thought":
        return "generation.thinking", "thought", {}
    if kind == "tool_use":
        category = tool_category(event.get("tool_name"))
        return "tool.started", category if category in {"search", "memory"} else "generating", {"category": category}
    if kind == "tool_result":
        return ("tool.failed", "error", {}) if event.get("is_error") else ("tool.completed", "generating", {})
    if kind in {"turn_end", "result"}:
        return "generation.completed", "success", {"outcome": "success"}
    if kind == "error":
        return "provider.failed", "provider_error", {}
    return None


class OverlayEventPublisher:
    """Best-effort external renderer publisher; failures keep the bundled renderer alive."""
    def __init__(self, cfg: dict | None = None, *, on_failure: Callable[[], None] | None = None, on_message: Callable[[dict[str, Any]], None] | None = None):
        ext = ((cfg or {}).get("overlay") or {}).get("external_renderer") or {}
        self._command = ext.get("command") if isinstance(ext, dict) else []
        self._enabled = bool(isinstance(self._command, list) and self._command)
        self.mode = str(ext.get("mode", "observer")).lower() if isinstance(ext, dict) else "observer"
        if self.mode not in {"observer", "replace"}:
            self.mode = "observer"
        self._proc: subprocess.Popen | None = None
        self._sequence = 0
        self._failed = False
        self._on_failure = on_failure
        self._inbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._on_message = on_message

    def start(self) -> bool:
        if not self._enabled:
            return False
        if not all(isinstance(item, str) and item.strip() for item in self._command):
            self._fail("command must be a non-empty string list")
            return False
        try:
            self._proc = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._fail(str(exc)); return False
        self._reader = threading.Thread(target=self._read_stdout, daemon=True, name="overlay-renderer-stdout")
        self._reader.start()
        try:
            data = self._inbound.get(timeout=2.0)
            if (
                data.get("type") != "overlay.hello"
                or SCHEMA_VERSION not in data.get("payload", {}).get("supported_schema_versions", [])
            ):
                raise ValueError("invalid hello")
        except Exception as exc:
            self._fail(f"handshake failed: {exc}")
            self.stop()
            return False
        self.publish("engram.welcome", "idle", {"selected_schema_version": SCHEMA_VERSION, "content_policy": "metadata_only"})
        self.publish("state.snapshot", "idle", {"generation_active": False, "tool_category": None})
        return True

    def _read_stdout(self) -> None:
        """Read JSONL without blocking the Tk thread; later input is retained for a future host adapter."""
        if not self._proc or not self._proc.stdout:
            return
        for raw in self._proc.stdout:
            if len(raw) > 65536:
                self._fail("renderer emitted an oversized JSONL message")
                self.stop()
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self._fail("renderer emitted invalid JSONL")
                self.stop()
                return
            if isinstance(message, dict):
                self._inbound.put(message)
                if self._on_message is not None:
                    self._on_message(message)
        if not self._failed and self._proc is not None and self._proc.poll() is not None:
            self._fail("renderer stdout closed")

    def publish(self, type: str, display_hint: str, payload: dict[str, Any] | None = None) -> None:
        if self._failed or not self._proc or self._proc.poll() is not None:
            return
        self._sequence += 1
        message = {"schema_version": SCHEMA_VERSION, "id": f"evt_{uuid.uuid4().hex}", "sequence": self._sequence,
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(), "type": type,
            "display_hint": display_hint if display_hint in DISPLAY_HINTS else "idle", "payload": payload or {}}
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n"); self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"write failed: {exc}")
            self.stop()

    def publish_bubble(self, event: object) -> None:
        mapped = event_for_bubble(event)
        if mapped:
            self.publish(*mapped)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
            except OSError:
                pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=0.2)
        self._reader = None

    def _fail(self, reason: str) -> None:
        if self._failed:
            return
        self._failed = True
        log.warning("[overlay-api] external renderer disabled; bundled renderer remains active: %s", reason)
        if self._on_failure:
            self._on_failure()
