"""Metadata-only readiness event hub shared by overlay lifecycle and STM HTTP."""
from __future__ import annotations
import threading

class McpRecoveryEvents:
    def __init__(self, instance_id: str):
        self._condition = threading.Condition(); self._revision = 0; self.closed = False
        self._state = {"instance_id": instance_id, "generation": 0, "ready": False, "endpoint": None}
    def snapshot(self):
        with self._condition: return {**self._state, "revision": self._revision}
    def publish(self, *, ready: bool, endpoint: str | None = None, replaced: bool = False):
        with self._condition:
            next_state = {**self._state, "generation": self._state["generation"] + (1 if replaced else 0),
                          "ready": bool(ready), "endpoint": endpoint if ready else None}
            if next_state != self._state:
                self._state = next_state; self._revision += 1; self._condition.notify_all()
            return {**self._state, "revision": self._revision}
    def wait_after(self, revision: int, timeout: float):
        with self._condition:
            if self._revision <= revision: self._condition.wait(timeout)
            return {**self._state, "revision": self._revision}

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()
