import json
import socket
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import pytest

from overlay.event_api import MAX_MESSAGE_BYTES, OverlayEventPublisher, _Client

_SOCKET_BUFFERS = {}


def _line(sock):
    key = sock.fileno()
    data = _SOCKET_BUFFERS.pop(key, b"")
    while b"\n" not in data:
        part = sock.recv(65536)
        if not part:
            return None
        data += part
    line, remainder = data.split(b"\n", 1)
    if remainder:
        _SOCKET_BUFFERS[key] = remainder
    return json.loads(line)


def _connect(discovery, renderer_id, modes, catalog=None):
    info = json.loads(discovery.read_text(encoding="utf-8"))
    sock = socket.create_connection((info["host"], info["port"]), timeout=2)
    sock.settimeout(2)
    registration = {"schema_version": 2, "type": "overlay.register", "payload": {
        "token": info["token"], "instance_id": info["instance_id"],
        "renderer_id": renderer_id, "name": renderer_id,
        "supported_modes": modes, "capabilities": ["overlay.presentation"],
    }}
    if catalog is not None:
        registration["payload"]["catalog"] = catalog
    sock.sendall((json.dumps(registration) + "\n").encode())
    welcome, snapshot, assignment = _line(sock), _line(sock), _line(sock)
    return sock, welcome, snapshot, assignment


def test_v2_auth_roles_routing_source_and_duplicate_policy():
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        inbound = []
        pub = OverlayEventPublisher(
            {"overlay": {"external_renderer": {"selected_renderer_id": "replace", "mode": "replace"}}},
            on_message=inbound.append, discovery_path=discovery,
        )
        pub.start()
        observer = replace = duplicate = None
        try:
            observer, ow, _, oa = _connect(discovery, "observer", ["observer"])
            replace, rw, _, ra = _connect(discovery, "replace", ["observer", "replace"])
            assert ow["payload"]["mode"] == "observer"
            assert rw["payload"]["mode"] == "replace"
            assert ow["payload"]["selected"] is False
            assert rw["payload"]["selected"] is True
            assert oa["payload"] == {"mode": "observer", "selected": False}
            assert ra["payload"] == {"mode": "replace", "selected": True}

            observer.sendall((json.dumps({"schema_version": 2, "type": "overlay.geometry_changed", "payload": {"x": 1, "y": 2, "width": 3, "height": 4}}) + "\n").encode())
            deadline = time.time() + 2
            while time.time() < deadline and not any(m.get("type") == "overlay.geometry_changed" for m in inbound):
                time.sleep(.01)
            geometry = next(m for m in inbound if m.get("type") == "overlay.geometry_changed")
            assert geometry["_renderer"] == {"id": "observer", "mode": "observer"}

            pub.publish("overlay.hide", "idle", {})
            assert _line(replace)["type"] == "overlay.hide"
            observer.settimeout(.1)
            try:
                unexpected = observer.recv(1)
            except socket.timeout:
                unexpected = b""
            assert unexpected == b""

            pub.set_selection("observer", "observer")
            observer_assignment = _line(observer)
            replace_assignment = _line(replace)
            assert observer_assignment["payload"] == {"mode": "observer", "selected": True}
            assert replace_assignment["payload"] == {"mode": "observer", "selected": False}
            assert pub.mode == "observer"
            pub.set_selection("replace", "replace")
            assert _line(observer)["payload"]["mode"] == "observer"
            assert _line(replace)["payload"] == {"mode": "replace", "selected": True}
            assert pub.mode == "replace"

            duplicate, _, _, _ = _connect(discovery, "observer", ["observer"])
            assert _line(duplicate) is None
        finally:
            for sock in (observer, replace, duplicate):
                if sock:
                    sock.close()
            pub.stop()


def test_catalog_provider_expands_logical_renderers_routes_selection_and_uses_item_capabilities():
    catalog = [
        {"renderer_id": "engram.rabbit-2d", "name": "Rabbit", "supported_modes": ["observer", "replace"],
         "capabilities": ["overlay.presentation"]},
        {"renderer_id": "engram.xeyes", "name": "Engram XEyes", "supported_modes": ["observer", "replace"],
         "capabilities": ["overlay.presentation", "future.item-capability"]},
    ]
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        inbound = []
        pub = OverlayEventPublisher(
            {"overlay": {"external_renderer": {"selected_renderer_id": "engram.rabbit-2d", "mode": "replace"}}},
            on_message=inbound.append, discovery_path=discovery,
        )
        pub.start()
        provider = collision = None
        try:
            provider, welcome, _, assignment = _connect(
                discovery, "engram.catalog-provider", ["observer", "replace"], catalog,
            )
            assert welcome["payload"]["selected"] is True
            assert welcome["payload"]["mode"] == "observer"
            assert assignment["payload"] == {
                "mode": "replace", "selected": True, "renderer_id": "engram.rabbit-2d",
            }
            assert [item["id"] for item in pub.connected_renderers()] == ["engram.rabbit-2d", "engram.xeyes"]
            assert pub.mode == "observer"
            assert not pub.supports("overlay.presentation")

            provider.sendall((json.dumps({
                "schema_version": 2, "type": "renderer.ready",
                "payload": {"renderer_id": "engram.rabbit-2d"},
            }) + "\n").encode())
            deadline = time.time() + 2
            while time.time() < deadline and pub.mode != "replace":
                time.sleep(.01)
            assert pub.mode == "replace"
            assert pub.supports("overlay.presentation")

            pub.publish("overlay.hide", "idle", {})
            assert _line(provider)["type"] == "overlay.hide"
            provider.sendall((json.dumps({
                "schema_version": 2, "type": "overlay.visibility_changed",
                "payload": {"visible": False},
            }) + "\n").encode())
            deadline = time.time() + 2
            while time.time() < deadline and not any(m.get("type") == "overlay.visibility_changed" for m in inbound):
                time.sleep(.01)
            hidden = next(m for m in inbound if m.get("type") == "overlay.visibility_changed")
            assert hidden["_renderer"] == {"id": "engram.rabbit-2d", "mode": "replace"}

            pub.set_selection("engram.xeyes", "replace")
            switched = _line(provider)
            assert switched["payload"] == {
                "mode": "replace", "selected": True, "renderer_id": "engram.xeyes",
            }
            assert pub.mode == "observer"
            provider.sendall((json.dumps({
                "schema_version": 2, "type": "renderer.ready",
                "payload": {"renderer_id": "engram.rabbit-2d"},
            }) + "\n").encode())
            time.sleep(.05)
            assert pub.mode == "observer", "stale readiness must not promote the previous item"
            provider.sendall((json.dumps({
                "schema_version": 2, "type": "renderer.ready",
                "payload": {"renderer_id": "engram.xeyes"},
            }) + "\n").encode())
            deadline = time.time() + 2
            while time.time() < deadline and pub.mode != "replace":
                time.sleep(.01)
            assert pub.mode == "replace"
            assert pub.supports("future.item-capability")
            provider.sendall((json.dumps({
                "schema_version": 2, "type": "pointer.action", "payload": {"action": "pointer_enter"},
            }) + "\n").encode())
            deadline = time.time() + 2
            while time.time() < deadline and not any(m.get("type") == "pointer.action" for m in inbound):
                time.sleep(.01)
            pointer = next(m for m in inbound if m.get("type") == "pointer.action")
            assert pointer["_renderer"] == {"id": "engram.xeyes", "mode": "replace"}

            collision, _, _, _ = _connect(discovery, "engram.rabbit-2d", ["observer"])
            assert _line(collision) is None
        finally:
            for sock in (provider, collision):
                if sock:
                    sock.close()
            pub.stop()


def test_stale_credentials_and_legacy_command_are_never_executed():
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        pub = OverlayEventPublisher(
            {"overlay": {"external_renderer": {"command": ["never.exe"], "mode": "replace"}}},
            discovery_path=discovery,
        )
        assert pub.legacy_diagnostic
        pub.start()
        try:
            info = json.loads(discovery.read_text(encoding="utf-8"))
            sock = socket.create_connection((info["host"], info["port"]), timeout=2)
            bad = {"schema_version": 2, "type": "overlay.register", "payload": {
                "token": "stale", "instance_id": info["instance_id"], "renderer_id": "bad",
                "name": "bad", "supported_modes": ["observer"],
            }}
            sock.sendall((json.dumps(bad) + "\n").encode())
            assert _line(sock) is None
            sock.close()
        finally:
            pub.stop()


@pytest.mark.parametrize("payload_patch", [
    {"name": None},
    {"name": ""},
    {"name": "bad\nname"},
    {"supported_modes": ["observer", "bogus"]},
    {"supported_modes": ["observer", "observer"]},
    {"supported_modes": ["observer", 1]},
    {"capabilities": "overlay.presentation"},
    {"capabilities": ["overlay.presentation", 1]},
    {"capabilities": ["overlay.presentation", "overlay.presentation"]},
    {"capabilities": ["x" * 129]},
    {"catalog": "not-a-list"},
    {"catalog": [{"renderer_id": "bad/id", "name": "Bad", "supported_modes": ["observer"]}]},
    {"catalog": [{"renderer_id": "same", "name": "A", "supported_modes": ["observer"]},
                 {"renderer_id": "same", "name": "B", "supported_modes": ["observer"]}]},
    {"catalog": [{"renderer_id": "good", "name": "Good", "supported_modes": ["observer"], "extra": True}]},
])
def test_registration_rejects_malformed_identity_modes_and_capabilities(payload_patch):
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        pub = OverlayEventPublisher(discovery_path=discovery)
        pub.start()
        sock = None
        try:
            info = json.loads(discovery.read_text(encoding="utf-8"))
            sock = socket.create_connection((info["host"], info["port"]), timeout=2)
            sock.settimeout(2)
            payload = {
                "token": info["token"], "instance_id": info["instance_id"],
                "renderer_id": "strict-client", "name": "Strict Client",
                "supported_modes": ["observer"], "capabilities": ["future.capability"],
            }
            payload.update(payload_patch)
            sock.sendall((json.dumps({"schema_version": 2, "type": "overlay.register", "payload": payload}) + "\n").encode())
            assert _line(sock) is None
            assert not pub.connected
        finally:
            if sock:
                sock.close()
            pub.stop()


def test_discovery_rotates_and_pending_registration_counts_toward_limit():
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        first = OverlayEventPublisher(discovery_path=discovery, max_connections=1)
        first.start()
        initial = json.loads(discovery.read_text(encoding="utf-8"))
        pending = socket.create_connection((initial["host"], initial["port"]), timeout=2)
        rejected = socket.create_connection((initial["host"], initial["port"]), timeout=2)
        rejected.settimeout(1)
        time.sleep(.05)
        assert rejected.recv(1) == b""
        rejected.close()
        pending.close()
        first.stop()

        second = OverlayEventPublisher(discovery_path=discovery)
        second.start()
        try:
            current = json.loads(discovery.read_text(encoding="utf-8"))
            assert current["instance_id"] != initial["instance_id"]
            assert current["token"] != initial["token"]
            assert current["host"] == "127.0.0.1"
        finally:
            second.stop()


def test_inbound_payload_schema_is_exact_and_role_authority_is_enforced():
    observer = _Client(None, "o", "Observer", "observer", ("observer",), frozenset())
    replace = _Client(None, "r", "Replace", "replace", ("replace",), frozenset())

    def valid(kind, payload, client=replace):
        return OverlayEventPublisher._valid_inbound(
            {"schema_version": 2, "type": kind, "payload": payload}, client
        )

    assert valid("overlay.geometry_changed", {"x": -10, "y": 20, "width": 1, "height": 100000})
    assert not valid("overlay.geometry_changed", {"x": 0, "y": 0, "width": 0, "height": 1})
    assert not valid("overlay.geometry_changed", {"x": 0.5, "y": 0, "width": 1, "height": 1})
    assert not valid("overlay.geometry_changed", {"x": 0, "y": 0, "width": 1, "height": 1, "text": "x"})
    assert valid("pointer.action", {"action": "left_click"})
    assert valid("pointer.action", {"action": "right_click", "screen_x": 1, "screen_y": 2})
    assert not valid("pointer.action", {"action": "right_click", "screen_x": 1})
    assert not valid("pointer.action", {"action": "left_click", "screen_x": 1, "screen_y": 2})
    assert not valid("pointer.action", {"action": "drag_end", "screen_x": [], "screen_y": 2})
    assert valid("overlay.visibility_changed", {"visible": False}, replace)
    assert not valid("overlay.visibility_changed", {"visible": False}, observer)
    assert not valid("overlay.visibility_changed", {"visible": 0}, replace)
    assert valid("overlay.heartbeat", {})
    assert not valid("overlay.heartbeat", {"nonce": "extra"})


def test_windows_acl_is_hardened_before_secret_payload_is_written():
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"

        def inspect_empty_temp(argv, **_kwargs):
            assert argv[0] == "icacls"
            assert Path(argv[1]).read_bytes() == b""
            return SimpleNamespace(returncode=0)

        pub = OverlayEventPublisher(discovery_path=discovery)
        with patch("overlay.event_api.subprocess.run", side_effect=inspect_empty_temp):
            pub._write_discovery(12345)
        payload = json.loads(discovery.read_text(encoding="utf-8"))
        assert payload["token"]
        discovery.unlink()


def test_oversize_jsonl_line_is_rejected_deterministically():
    left, right = socket.socketpair()
    try:
        right.sendall(b"{" + b"x" * MAX_MESSAGE_BYTES + b"\n")
        with pytest.raises(ValueError, match="message too large"):
            OverlayEventPublisher._read_line(left, bytearray())
    finally:
        left.close()
        right.close()


def test_snapshot_and_layer_removal_preserve_current_public_work_state():
    with tempfile.TemporaryDirectory() as td:
        discovery = Path(td) / "discovery.json"
        pub = OverlayEventPublisher(discovery_path=discovery)
        pub.publish("generation.started", "generating", {})
        pub.publish("tool.started", "search", {"category": "search"})
        pub.publish("conversation.input_active", "input", {})
        pub.publish("conversation.input_idle", "idle", {})
        pub.publish("pointer.entered", "hover", {})
        pub.publish("pointer.left", "idle", {})
        pub.start()
        sock = None
        try:
            sock, welcome, snapshot, assignment = _connect(discovery, "observer", ["observer"])
            assert welcome["display_hint"] == "search"
            assert snapshot["display_hint"] == "search"
            assert snapshot["payload"] == {"generation_active": True, "tool_category": "search"}
            assert assignment["display_hint"] == "search"
        finally:
            if sock:
                sock.close()
            pub.stop()


def test_controls_and_layer_removal_emit_resolved_hint_not_idle():
    pub = OverlayEventPublisher()
    sent = []
    client = _Client(None, "r", "Replace", "replace", ("replace",), frozenset())
    # Outbound is queued now so the Tk thread never touches the socket; capture
    # at the queue boundary instead of at sendall.
    client.enqueue = lambda message, *, droppable: sent.append(message)
    pub._clients[id(client)] = client
    pub._replace_owner = id(client)

    pub.publish("generation.started", "generating", {})
    pub.publish("pointer.entered", "hover", {})
    pub.publish("pointer.left", "idle", {})
    pub.publish("overlay.hide", "idle", {})
    pub.set_selection("r", "replace")

    assert [message["display_hint"] for message in sent] == [
        "generating", "hover", "generating", "generating", "generating"
    ]


def test_completed_tool_is_not_reported_as_still_active_in_a_later_snapshot():
    pub = OverlayEventPublisher()
    pub.publish("generation.started", "generating", {})
    pub.publish("tool.started", "search", {"category": "search"})
    pub.publish("tool.completed", "generating", {})

    assert pub._resolved_hint() == "generating"
    assert pub._generation_active is True
    assert pub._tool_category is None
