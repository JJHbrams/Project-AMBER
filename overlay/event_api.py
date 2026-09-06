"""Authenticated loopback Event API v2 host for independently started renderers."""
from __future__ import annotations

import datetime as _dt
import getpass
import collections
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)
SCHEMA_VERSION = 2
DISPLAY_HINTS = frozenset({"default", "idle", "hover", "click", "input", "generating", "search", "thought", "memory", "success", "provider_error", "error"})
INBOUND_TYPES = frozenset({"overlay.geometry_changed", "overlay.visibility_changed", "pointer.action", "overlay.heartbeat"})
POINTER_ACTIONS = frozenset({"left_click", "right_click", "pointer_enter", "pointer_leave", "drag_begin", "drag_move", "drag_end", "overlay_close", "menu_dismiss"})
REPLACE_CONTROL_TYPES = frozenset({"overlay.set_position", "overlay.set_size", "overlay.show", "overlay.hide"})
_TRANSIENT_TYPES = frozenset({"pointer.left_clicked", "pointer.right_clicked", "tool.failed", "generation.completed", "provider.failed"})
MAX_MESSAGE_BYTES = 65_536
MAX_CONNECTIONS = 16
REGISTRATION_TIMEOUT_SECONDS = 2.0
MAX_MESSAGES_PER_SECOND = 120
MAX_CAPABILITIES = 32
MAX_CAPABILITY_LENGTH = 128
MAX_CATALOG_ITEMS = 32
MAX_OUTBOUND_MESSAGES = 256
_ACTIVE_HOST: "OverlayEventPublisher | None" = None


def discovery_file(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".engram" / "overlay-event-api-v2.json"


def tool_category(name: object) -> str:
    value = str(name or "").lower()
    if any(x in value for x in ("memory", "kg_", "recall")): return "memory"
    if any(x in value for x in ("search", "find", "web", "browser", "fetch", "grep", "glob", "list")): return "search"
    if any(x in value for x in ("read", "open")): return "read"
    if any(x in value for x in ("write", "edit", "patch", "delete")): return "write"
    # "bash" contains none of the generic verbs, so the most common shell tool
    # fell through to "other" while PowerShell matched on "shell".
    if any(x in value for x in ("shell", "bash", "exec", "build", "test", "run", "task", "agent")): return "execute"
    if any(x in value for x in ("mail", "message", "discord", "slack")): return "communication"
    return "other"


def _loopback_port_owner(port: int) -> int | None:
    """PID owning a local IPv4 TCP port, or None when it cannot be determined."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class _Row(ctypes.Structure):
            _fields_ = [("state", wintypes.DWORD), ("local_addr", wintypes.DWORD),
                        ("local_port", wintypes.DWORD), ("remote_addr", wintypes.DWORD),
                        ("remote_port", wintypes.DWORD), ("owning_pid", wintypes.DWORD)]

        size = wintypes.DWORD(0)
        get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
        # AF_INET = 2, TCP_TABLE_OWNER_PID_ALL = 5
        get_table(None, ctypes.byref(size), False, 2, 5, 0)
        buffer = ctypes.create_string_buffer(size.value)
        if get_table(buffer, ctypes.byref(size), False, 2, 5, 0) != 0:
            return None
        count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        rows = ctypes.cast(ctypes.byref(buffer, ctypes.sizeof(wintypes.DWORD)), ctypes.POINTER(_Row))
        wanted = socket.htons(port) & 0xFFFF
        for index in range(count):
            row = rows[index]
            if (row.local_port & 0xFFFF) == wanted:
                return int(row.owning_pid)
    except Exception:
        return None
    return None


def event_for_bubble(event: object) -> tuple[str, str, dict[str, Any]] | None:
    if not isinstance(event, dict): return None
    kind = str(event.get("kind") or "").lower()
    if kind == "thought": return "generation.thinking", "thought", {}
    if kind == "tool_use":
        category = tool_category(event.get("tool_name"))
        return "tool.started", category if category in {"search", "memory"} else "generating", {"category": category}
    if kind == "tool_result": return ("tool.failed", "error", {}) if event.get("is_error") else ("tool.completed", "generating", {})
    if kind in {"turn_end", "result"}: return "generation.completed", "success", {"outcome": "success"}
    if kind == "error": return "provider.failed", "provider_error", {}
    return None


class _CatalogItem:
    def __init__(self, renderer_id: str, name: str, supported_modes: tuple[str, ...], capabilities: frozenset[str]):
        self.renderer_id = renderer_id
        self.name = name
        self.supported_modes = supported_modes
        self.capabilities = capabilities


class _Client:
    def __init__(self, connection: socket.socket, renderer_id: str, name: str, mode: str,
                 supported_modes: tuple[str, ...], capabilities: frozenset[str],
                 catalog: tuple[_CatalogItem, ...] = ()):
        self.connection, self.renderer_id, self.name = connection, renderer_id, name
        self.mode, self.supported_modes, self.capabilities, self.lock = mode, supported_modes, capabilities, threading.Lock()
        self.catalog = catalog
        self.active_renderer_id: str | None = renderer_id if not catalog else None
        self.pending_renderer_id: str | None = None
        # Outbound is queued so a renderer that stops reading can never block the
        # host's Tk main thread inside sendall.  Semantic events are droppable;
        # control and handshake messages are not, because losing one leaves the
        # renderer's window state permanently out of sync with the host.
        self._outbound: "collections.deque[tuple[bool, bytes]]" = collections.deque()
        self._outbound_ready = threading.Event()
        self._outbound_lock = threading.Lock()
        self._outbound_closed = False
        self._writer: threading.Thread | None = None
        self.dropped_events = 0

    def item(self, renderer_id: str) -> _CatalogItem | None:
        if not self.catalog:
            return _CatalogItem(self.renderer_id, self.name, self.supported_modes, self.capabilities) if renderer_id == self.renderer_id else None
        return next((item for item in self.catalog if item.renderer_id == renderer_id), None)

    def advertised_ids(self) -> frozenset[str]:
        return frozenset({self.renderer_id, *(item.renderer_id for item in self.catalog)})

    def assignment_payload(self, selected_renderer_id: str, selected_mode: str) -> dict[str, Any]:
        selected = self.item(selected_renderer_id) is not None
        item = self.item(selected_renderer_id)
        assigned_mode = selected_mode if item is not None and selected_mode in item.supported_modes else "observer"
        payload: dict[str, Any] = {"mode": assigned_mode if selected else "observer", "selected": selected}
        if self.catalog and selected:
            payload["renderer_id"] = selected_renderer_id
        return payload

    def send(self, message: dict[str, Any]) -> None:
        """Blocking send. Only registration/handshake paths may use this."""
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock: self.connection.sendall(data)

    def enqueue(self, message: dict[str, Any], *, droppable: bool) -> None:
        """Hand a message to this client's writer thread and return immediately."""
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._outbound_lock:
            if self._outbound_closed:
                return
            if len(self._outbound) >= MAX_OUTBOUND_MESSAGES:
                # Shed the oldest droppable event.  A stalled renderer misses
                # animation cues rather than a position or visibility command.
                for index, (candidate_droppable, _payload) in enumerate(self._outbound):
                    if candidate_droppable:
                        del self._outbound[index]
                        self.dropped_events += 1
                        break
                else:
                    if droppable:
                        self.dropped_events += 1
                        return
            self._outbound.append((droppable, data))
            if self._writer is None:
                self._writer = threading.Thread(target=self._writer_loop, daemon=True,
                                                name=f"overlay-out-{self.renderer_id}")
                self._writer.start()
        self._outbound_ready.set()

    def close_outbound(self) -> None:
        with self._outbound_lock:
            self._outbound_closed = True
            self._outbound.clear()
        self._outbound_ready.set()

    def _writer_loop(self) -> None:
        while True:
            self._outbound_ready.wait()
            with self._outbound_lock:
                if self._outbound_closed:
                    return
                if not self._outbound:
                    self._outbound_ready.clear()
                    continue
                _droppable, data = self._outbound[0]
            try:
                with self.lock:
                    self.connection.sendall(data)
            except socket.timeout:
                # The renderer is briefly not reading.  Blocking is harmless off
                # the Tk thread, so retry instead of tearing down a renderer
                # that is merely busy compositing a frame.
                continue
            except OSError:
                # The reader loop owns teardown; stop writing to a dead socket.
                self.close_outbound()
                return
            with self._outbound_lock:
                if self._outbound and self._outbound[0][1] is data:
                    self._outbound.popleft()


class OverlayEventPublisher:
    """Own only the v2 socket host; renderer process lifetime stays client-owned."""
    def __init__(self, cfg: dict | None = None, *, on_failure: Callable[[], None] | None = None,
                 on_message: Callable[[dict[str, Any]], None] | None = None,
                 discovery_path: Path | None = None, max_connections: int = MAX_CONNECTIONS):
        ext = ((cfg or {}).get("overlay") or {}).get("external_renderer") or {}
        self.selected_renderer_id = str(ext.get("selected_renderer_id") or "") if isinstance(ext, dict) else ""
        self.selected_mode = str(ext.get("mode") or "observer").lower() if isinstance(ext, dict) else "observer"
        if self.selected_mode not in {"observer", "replace"}: self.selected_mode = "observer"
        self.legacy_diagnostic = "legacy command configuration is disabled; select a connected renderer" if isinstance(ext, dict) and ext.get("command") else ""
        self.mode, self.capabilities = "observer", frozenset()
        self._on_failure, self._on_message = on_failure, on_message
        self._discovery_path = discovery_path or discovery_file()
        self._max_connections = max(1, min(int(max_connections), MAX_CONNECTIONS))
        self._instance_id, self._token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        self._listener: socket.socket | None = None
        self._clients: dict[int, _Client] = {}
        self._replace_owner: int | None = None
        self._connection_slots = 0
        self._lock, self._stopping, self._sequence = threading.RLock(), threading.Event(), 0
        # The host owns the semantic state.  Keep the durable layers separately
        # so a pointer-leave or typing-idle event can reveal active work instead
        # of incorrectly resetting an external renderer to idle.
        self._work_hint = "idle"
        self._tool_category: str | None = None
        self._generation_active = False
        self._hovered = False
        self._input_active = False

    @property
    def connected(self) -> bool:
        with self._lock: return bool(self._clients)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def start(self) -> bool:
        global _ACTIVE_HOST
        if self._listener is not None: return True
        if _ACTIVE_HOST is not None and _ACTIVE_HOST is not self:
            raise RuntimeError("an Event API v2 host is already active in this process")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0)); listener.listen(self._max_connections); listener.settimeout(0.25)
        self._listener = listener
        try:
            self._write_discovery(listener.getsockname()[1])
        except Exception:
            self._listener = None
            listener.close()
            raise
        _ACTIVE_HOST = self
        threading.Thread(target=self._accept_loop, daemon=True, name="overlay-event-api-v2").start()
        return True

    def _write_discovery(self, port: int) -> None:
        self._discovery_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 2, "host": "127.0.0.1", "port": port, "instance_id": self._instance_id, "token": self._token}
        fd, temp_name = tempfile.mkstemp(prefix=".overlay-api-", dir=self._discovery_path.parent); temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            if os.name == "nt":
                domain = os.environ.get("USERDOMAIN", "").strip()
                account = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
                result = subprocess.run(["icacls", str(temp_path), "/inheritance:r", "/grant:r", f"{account}:F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0: raise OSError("unable to protect discovery file")
            elif stat.S_IMODE(temp_path.stat().st_mode) & 0o077:
                raise OSError("discovery file permissions are not private")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(payload, stream, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp_path, self._discovery_path)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path.exists(): temp_path.unlink()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try: connection, _ = self._listener.accept() if self._listener else (None, None)
            except socket.timeout: continue
            except OSError: break
            if connection is None: continue
            with self._lock:
                if self._connection_slots >= self._max_connections: connection.close(); continue
                self._connection_slots += 1
            threading.Thread(target=self._client_loop, args=(connection,), daemon=True, name="overlay-event-client").start()

    @staticmethod
    def _read_line(connection: socket.socket, buffer: bytearray) -> dict[str, Any] | None:
        while b"\n" not in buffer:
            chunk = connection.recv(4096)
            if not chunk: return None
            buffer.extend(chunk)
            if len(buffer) > MAX_MESSAGE_BYTES: raise ValueError("message too large")
        raw, _, remainder = buffer.partition(b"\n"); buffer[:] = remainder
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict): raise ValueError("message must be an object")
        return value

    def _client_loop(self, connection: socket.socket) -> None:
        client = None; buffer = bytearray(); key = id(connection); was_replace = False
        try:
            connection.settimeout(REGISTRATION_TIMEOUT_SECONDS)
            client = self._register(connection, self._read_line(connection, buffer))
            if client is None: return
            connection.settimeout(1.0); self._send_welcome(client)
            window_started, messages = time.monotonic(), 0
            while not self._stopping.is_set():
                try: message = self._read_line(connection, buffer)
                except socket.timeout: continue
                if message is None: break
                now = time.monotonic()
                if now - window_started >= 1.0: window_started, messages = now, 0
                messages += 1
                if messages > MAX_MESSAGES_PER_SECOND: raise ValueError("message rate exceeded")
                if message.get("type") == "renderer.ready":
                    self._handle_ready(client, message)
                    continue
                if self._valid_inbound(message, client) and self._on_message:
                    tagged = dict(message); tagged["_renderer"] = {"id": client.active_renderer_id or client.renderer_id, "mode": client.mode}
                    self._on_message(tagged)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError): pass
        finally:
            with self._lock:
                removed = self._clients.pop(key, None); was_replace = self._replace_owner == key
                if was_replace: self._replace_owner = None; self.mode = "observer"; self.capabilities = frozenset()
                self._connection_slots = max(0, self._connection_slots - 1)
            if removed is not None: removed.close_outbound()
            try: connection.close()
            except OSError: pass
            if removed and removed.active_renderer_id and self._on_message:
                self._on_message({"type": "_renderer.disconnected", "payload": {"renderer_id": removed.active_renderer_id, "mode": removed.mode}})
            if was_replace and self._on_failure: self._on_failure()

    def _register(self, connection: socket.socket, message: dict[str, Any] | None) -> _Client | None:
        payload = message.get("payload") if isinstance(message, dict) else None
        if not isinstance(payload, dict) or message.get("schema_version") != 2 or message.get("type") != "overlay.register": return None
        if not secrets.compare_digest(str(payload.get("token") or ""), self._token) or payload.get("instance_id") != self._instance_id: return None
        renderer_id = str(payload.get("renderer_id") or "")
        if not renderer_id or len(renderer_id) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in renderer_id): return None
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name): return None
        modes = payload.get("supported_modes")
        if (not isinstance(modes, list) or not modes or
                any(not isinstance(mode, str) or mode not in {"observer", "replace"} for mode in modes) or
                len(set(modes)) != len(modes)):
            return None
        supported_modes = tuple(modes)
        raw_capabilities = payload.get("capabilities", [])
        if (not isinstance(raw_capabilities, list) or len(raw_capabilities) > MAX_CAPABILITIES or
                any(not isinstance(capability, str) or not capability or
                    len(capability) > MAX_CAPABILITY_LENGTH or
                    any(ord(c) < 32 or ord(c) == 127 for c in capability)
                    for capability in raw_capabilities) or
                len(set(raw_capabilities)) != len(raw_capabilities)):
            return None
        capabilities = frozenset(raw_capabilities)
        raw_catalog = payload.get("catalog", [])
        if not isinstance(raw_catalog, list) or len(raw_catalog) > MAX_CATALOG_ITEMS:
            return None
        catalog: list[_CatalogItem] = []
        catalog_ids: set[str] = set()
        for raw_item in raw_catalog:
            if not isinstance(raw_item, dict) or set(raw_item) not in (
                    {"renderer_id", "name", "supported_modes"},
                    {"renderer_id", "name", "supported_modes", "capabilities"}):
                return None
            item_id = raw_item.get("renderer_id")
            item_name = raw_item.get("name")
            item_modes = raw_item.get("supported_modes")
            item_capabilities = raw_item.get("capabilities", [])
            if (not isinstance(item_id, str) or not item_id or len(item_id) > 64 or
                    any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in item_id) or
                    item_id == renderer_id or item_id in catalog_ids):
                return None
            if (not isinstance(item_name, str) or not item_name.strip() or len(item_name) > 128 or
                    any(ord(c) < 32 or ord(c) == 127 for c in item_name)):
                return None
            if (not isinstance(item_modes, list) or not item_modes or
                    any(not isinstance(mode, str) or mode not in {"observer", "replace"} for mode in item_modes) or
                    len(set(item_modes)) != len(item_modes)):
                return None
            if (not isinstance(item_capabilities, list) or len(item_capabilities) > MAX_CAPABILITIES or
                    any(not isinstance(capability, str) or not capability or
                        len(capability) > MAX_CAPABILITY_LENGTH or
                        any(ord(c) < 32 or ord(c) == 127 for c in capability)
                        for capability in item_capabilities) or
                    len(set(item_capabilities)) != len(item_capabilities)):
                return None
            catalog_ids.add(item_id)
            catalog.append(_CatalogItem(item_id, item_name, tuple(item_modes), frozenset(item_capabilities)))
        with self._lock:
            advertised_ids = {renderer_id, *catalog_ids}
            if any(advertised_ids & existing.advertised_ids() for existing in self._clients.values()): return None
            selected_item = next((item for item in catalog if item.renderer_id == self.selected_renderer_id), None)
            owns_selection = selected_item is not None or (not catalog and renderer_id == self.selected_renderer_id)
            # A singleton is already the renderer it registered as and retains
            # the immediate v2 behavior. A catalog provider must first prove its
            # newly selected hidden worker is ready; until then bundled fallback
            # remains authoritative and no controls can be routed to the socket.
            mode = "replace" if not catalog and owns_selection and self.selected_mode == "replace" and "replace" in supported_modes and self._replace_owner is None else "observer"
            if mode == "observer" and "observer" not in modes: return None
            client = _Client(connection, renderer_id, name, mode, supported_modes, capabilities, tuple(catalog))
            if catalog and owns_selection:
                client.pending_renderer_id = self.selected_renderer_id
            key = id(connection); self._clients[key] = client
            if mode == "replace": self._replace_owner = key; self.mode = "replace"; self.capabilities = capabilities
        active_item = client.item(client.active_renderer_id or "")
        if self._on_message and active_item is not None:
            self._on_message({"type": "_renderer.connected", "payload": {"renderer_id": active_item.renderer_id, "name": active_item.name, "mode": mode, "capabilities": sorted(active_item.capabilities)}})
        return client

    def _send_welcome(self, client: _Client) -> None:
        # Handshake goes through the same outbound queue as everything else so
        # a publish racing registration can never overtake welcome/snapshot.
        selected = client.item(self.selected_renderer_id) is not None
        self._enqueue(client, self._message("engram.welcome", self._resolved_hint(), {"selected_schema_version": 2, "content_policy": "metadata_only", "mode": client.mode, "selected": selected, "host_instance_id": self._instance_id}))
        self._enqueue(client, self._message("state.snapshot", self._resolved_hint(), {"generation_active": self._generation_active, "tool_category": self._tool_category}))
        # Welcome is intentionally self-contained, while the assignment event
        # makes initial selection follow the same path as later recomputation.
        self._enqueue(client, self._message("renderer.assignment", self._resolved_hint(), client.assignment_payload(self.selected_renderer_id, self.selected_mode)))

    def _handle_ready(self, client: _Client, message: dict[str, Any]) -> None:
        """Promote only the catalog item assigned on this authenticated socket."""
        payload = message.get("payload")
        if (message.get("schema_version") != 2 or not client.catalog or not isinstance(payload, dict) or
                set(payload) != {"renderer_id"} or not isinstance(payload.get("renderer_id"), str)):
            return
        renderer_id = payload["renderer_id"]
        connected = None
        with self._lock:
            item = client.item(renderer_id)
            if (item is None or client.pending_renderer_id != renderer_id or
                    self.selected_renderer_id != renderer_id or self.selected_mode not in item.supported_modes):
                return
            if self.selected_mode == "replace" and self._replace_owner not in (None, id(client.connection)):
                return
            client.pending_renderer_id = None
            client.active_renderer_id = renderer_id
            client.capabilities = item.capabilities
            client.mode = self.selected_mode
            if client.mode == "replace":
                key = id(client.connection)
                self._replace_owner = key
                self.mode = "replace"
                self.capabilities = item.capabilities
            connected = {"type": "_renderer.connected", "payload": {
                "renderer_id": item.renderer_id, "name": item.name,
                "mode": client.mode, "capabilities": sorted(item.capabilities),
            }}
        if connected is not None and self._on_message:
            self._on_message(connected)

    @staticmethod
    def _valid_inbound(message: dict[str, Any], client: _Client) -> bool:
        if client.catalog and client.active_renderer_id is None: return False
        if message.get("schema_version") != 2 or message.get("type") not in INBOUND_TYPES: return False
        payload = message.get("payload")
        if not isinstance(payload, dict): return False
        kind = message["type"]

        def bounded_int(value: object, limit: int = 1_000_000) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and -limit <= value <= limit

        if kind == "overlay.geometry_changed":
            if set(payload) != {"x", "y", "width", "height"}: return False
            return (
                bounded_int(payload["x"]) and bounded_int(payload["y"])
                and bounded_int(payload["width"], 100_000) and 1 <= payload["width"] <= 100_000
                and bounded_int(payload["height"], 100_000) and 1 <= payload["height"] <= 100_000
            )
        if kind == "overlay.visibility_changed":
            return client.mode == "replace" and set(payload) == {"visible"} and isinstance(payload["visible"], bool)
        if kind == "overlay.heartbeat":
            return not payload
        action = payload.get("action")
        if action not in POINTER_ACTIONS: return False
        coordinate_actions = {"right_click", "drag_move", "drag_end"}
        expected = {"action", "screen_x", "screen_y"} if action in coordinate_actions else {"action"}
        if set(payload) != expected: return False
        return action not in coordinate_actions or (
            bounded_int(payload["screen_x"]) and bounded_int(payload["screen_y"])
        )

    def _message(self, type: str, display_hint: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock: self._sequence += 1; sequence = self._sequence
        return {"schema_version": 2, "id": f"evt_{uuid.uuid4().hex}", "sequence": sequence, "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(), "type": type, "display_hint": display_hint if display_hint in DISPLAY_HINTS else "idle", "payload": payload or {}}

    def _resolved_hint(self) -> str:
        if self._input_active:
            return "input"
        if self._hovered:
            return "hover"
        return self._work_hint

    def _resolve_event_hint(self, type: str, display_hint: str, payload: dict[str, Any]) -> str:
        """Update metadata-only public state and return this event's hint.

        Transients are delivered as-is, but do not erase durable work.  Layer
        removal events resolve against the remaining layers, matching the
        bundled sprite reducer.
        """
        if type == "conversation.input_active":
            self._input_active = True
            return self._resolved_hint()
        if type in {"conversation.input_idle", "conversation.input_submitted"}:
            self._input_active = False
            return self._resolved_hint() if type == "conversation.input_idle" else "input"
        if type == "pointer.entered":
            self._hovered = True
            return self._resolved_hint()
        if type == "pointer.left":
            self._hovered = False
            return self._resolved_hint()
        if type == "generation.started":
            self._generation_active = True
            self._work_hint, self._tool_category = "generating", None
        elif type == "generation.thinking":
            self._generation_active = True
            self._work_hint = "thought"
        elif type == "tool.started":
            self._generation_active = True
            category = payload.get("category")
            self._tool_category = category if isinstance(category, str) else None
            self._work_hint = display_hint if display_hint in {"search", "memory"} else "generating"
        elif type == "tool.completed":
            self._work_hint = "generating" if self._generation_active else "idle"
            self._tool_category = None
        elif type == "tool.failed":
            self._tool_category = None
        elif type == "generation.completed":
            self._generation_active = False
            self._work_hint, self._tool_category = "idle", None
        elif type == "provider.failed":
            self._generation_active = False
            self._work_hint, self._tool_category = "idle", None

        if type in REPLACE_CONTROL_TYPES:
            return self._resolved_hint()
        if type in _TRANSIENT_TYPES:
            return display_hint if display_hint in DISPLAY_HINTS else self._resolved_hint()
        return self._resolved_hint()

    @staticmethod
    def _send(client: _Client, message: dict[str, Any]) -> bool:
        try: client.send(message); return True
        except OSError: return False

    @staticmethod
    def _enqueue(client: _Client, message: dict[str, Any]) -> None:
        client.enqueue(message, droppable=False)

    def publish(self, type: str, display_hint: str, payload: dict[str, Any] | None = None) -> None:
        """Queue an outbound message. Never blocks, so Tk cannot stall on a socket."""
        payload = payload or {}
        with self._lock:
            resolved_hint = self._resolve_event_hint(type, display_hint, payload)
        message = self._message(type, resolved_hint, payload)
        control = type in REPLACE_CONTROL_TYPES
        with self._lock:
            if control:
                owner = self._clients.get(self._replace_owner) if self._replace_owner is not None else None
                clients = [owner] if owner is not None else []
            else:
                clients = list(self._clients.values())
        for client in clients: client.enqueue(message, droppable=not control)

    def publish_bubble(self, event: object) -> None:
        mapped = event_for_bubble(event)
        if mapped: self.publish(*mapped)

    def replace_owner_pid(self) -> int | None:
        """PID of the process holding the replace connection, from the socket itself.

        The host deliberately never learns a renderer's path or command, but the
        loopback peer of an authenticated connection is unambiguous. Inferring
        it from "whatever owned the foreground when a click arrived" only worked
        if the user happened to click the character.
        """
        with self._lock:
            owner = self._clients.get(self._replace_owner) if self._replace_owner is not None else None
        if owner is None:
            return None
        try:
            peer_port = int(owner.connection.getpeername()[1])
        except OSError:
            return None
        return _loopback_port_owner(peer_port)

    def connected_renderers(self) -> list[dict[str, Any]]:
        with self._lock:
            renderers = []
            for client in self._clients.values():
                if client.catalog:
                    renderers.extend({"id": item.renderer_id, "name": item.name,
                                      "mode": client.mode if client.active_renderer_id == item.renderer_id else "observer",
                                      "supported_modes": item.supported_modes}
                                     for item in client.catalog)
                else:
                    renderers.append({"id": client.renderer_id, "name": client.name, "mode": client.mode,
                                      "supported_modes": client.supported_modes})
            return renderers

    def set_selection(self, renderer_id: str, mode: str) -> None:
        self.selected_renderer_id = renderer_id; self.selected_mode = mode if mode in {"observer", "replace"} else "observer"
        old_owner = None; old_owner_id = None; promoted = None
        with self._lock:
            if self._replace_owner is not None:
                owner = self._clients.get(self._replace_owner)
                if owner is not None:
                    old_owner = owner; old_owner_id = owner.active_renderer_id or owner.renderer_id; owner.mode = "observer"
                self._replace_owner = None; self.mode = "observer"; self.capabilities = frozenset()
            selected_client = None
            selected_item = None
            for client in self._clients.values():
                client.mode = "observer"
                if client.catalog:
                    client.active_renderer_id = None
                    client.pending_renderer_id = None
                item = client.item(renderer_id)
                if item is not None:
                    selected_client, selected_item = client, item
            if selected_client is not None and selected_item is not None and self.selected_mode in selected_item.supported_modes:
                if selected_client.catalog:
                    selected_client.pending_renderer_id = selected_item.renderer_id
                else:
                    selected_client.active_renderer_id = selected_item.renderer_id
                    selected_client.capabilities = selected_item.capabilities
                if self.selected_mode == "replace" and not selected_client.catalog:
                    selected_client.mode = "replace"
                    for key, client in self._clients.items():
                        if client is selected_client:
                            self._replace_owner = key
                            break
                    self.mode = "replace"; self.capabilities = selected_item.capabilities; promoted = selected_client
            clients = list(self._clients.values())
        if old_owner is not None and old_owner is not promoted and self._on_message:
            self._on_message({"type": "_renderer.disconnected", "payload": {"renderer_id": old_owner_id, "mode": "replace"}})
        for client in clients:
            # Settings applies a selection from the Tk thread; assignment is
            # never droppable but must also never block that thread.
            client.enqueue(self._message("renderer.assignment", self._resolved_hint(), client.assignment_payload(renderer_id, self.selected_mode)), droppable=False)
        if promoted is not None and selected_item is not None and self._on_message:
            self._on_message({"type": "_renderer.connected", "payload": {"renderer_id": selected_item.renderer_id, "name": selected_item.name, "mode": "replace", "capabilities": sorted(selected_item.capabilities)}})

    def stop(self) -> None:
        global _ACTIVE_HOST
        self._stopping.set(); listener, self._listener = self._listener, None
        if listener:
            try: listener.close()
            except OSError: pass
        with self._lock: clients = list(self._clients.values()); self._clients.clear(); self._replace_owner = None
        for client in clients:
            try: client.connection.close()
            except OSError: pass
        if _ACTIVE_HOST is self: _ACTIVE_HOST = None
        try:
            current = json.loads(self._discovery_path.read_text(encoding="utf-8"))
            if current.get("instance_id") == self._instance_id: self._discovery_path.unlink()
        except (OSError, ValueError, json.JSONDecodeError): pass


def connected_renderer_snapshot() -> list[dict[str, Any]]:
    return _ACTIVE_HOST.connected_renderers() if _ACTIVE_HOST else []
