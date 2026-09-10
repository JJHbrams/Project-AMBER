import asyncio
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.integrations.mcp_presence import (
    PresenceMiddleware,
    PresenceReporter,
    TransportIdentityFilter,
)
from overlay.session_registry import SessionStateRegistry
from overlay.state_api import validate_presence


class PresenceTests(unittest.TestCase):
    def test_explicit_state_uses_same_identity_and_reports_delivery_truthfully(self):
        reporter = PresenceReporter()
        request = SimpleNamespace(
            headers={"mcp-session-id": "transport-a"},
            scope={"engram.presence.transport_id": "transport-a"},
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        reporter.submit = Mock()
        reporter._send = Mock(return_value=True)
        reporter.report_context(context)
        result = reporter.report_state(context, "working", "safe alias", 3)
        identifier = reporter.submit.call_args.args[0]
        reporter._send.assert_called_once_with(
            {
                "provider": "mcp",
                "session_id": identifier,
                "state": "working",
                "label": "safe alias",
                "subagent_count": 3,
            },
            endpoint="/state",
        )
        self.assertEqual(result, {"accepted": True, "reason": "delivered"})
        self.assertNotIn(identifier, json.dumps(result))
        reporter._send.side_effect = TimeoutError()
        self.assertFalse(reporter.report_state(context, "ready")["accepted"])
        reporter._send.side_effect = None
        reporter._send.return_value = False
        self.assertFalse(reporter.report_state(context, "ready")["accepted"])

    def test_explicit_state_rejects_bubble_and_invalid_metadata(self):
        reporter = PresenceReporter()
        request = SimpleNamespace(
            headers={"mcp-session-id": "transport", "x-engram-bubble-owner": "a" * 32},
            scope={
                "engram.presence.local": True,
                "engram.presence.transport_id": "transport",
            },
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        reporter._send = Mock(return_value=True)
        self.assertEqual(
            reporter.report_state(context, "ready")["reason"], "bubble_controller_owned"
        )
        reporter._send.assert_not_called()
        request.scope["engram.presence.local"] = (
            False  # Remote forged owner header is ignored.
        )
        for arguments in (
            ("invalid", None, None),
            ("working", "C:/secret", None),
            ("ready", "x" * 129, None),
            ("working", None, True),
            ("ready", None, 10000),
        ):
            self.assertEqual(
                reporter.report_state(context, *arguments)["reason"], "invalid_metadata"
            )
        reporter._send.assert_not_called()
        self.assertTrue(reporter.report_state(context, "ready")["accepted"])
        self.assertEqual(reporter._send.call_args.args[0]["provider"], "mcp")

    def test_title_uses_same_context_allows_owned_bubble_but_never_sends_state(self):
        reporter = PresenceReporter()
        request = SimpleNamespace(
            headers={"mcp-session-id": "transport", "x-engram-bubble-owner": "a" * 32},
            scope={"engram.presence.local": True, "engram.presence.transport_id": "transport"},
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        reporter._send = Mock(return_value=True)
        self.assertEqual(reporter.report_title(context, "Safe generated title"),
                         {"accepted": True, "reason": "delivered"})
        self.assertEqual(reporter._send.call_args_list[0].args[0],
                         {"provider": "claude", "session_id": "a" * 32,
                          "bubble_owner": True, "ended": False})
        reporter._send.assert_called_with(
            {"provider": "claude", "session_id": "a" * 32, "title": "Safe generated title"},
            endpoint="/state/title")
        self.assertEqual(reporter.report_state(context, "ready")["reason"], "bubble_controller_owned")
        self.assertEqual(reporter.report_title(context, "one")["reason"], "invalid_metadata")

    def test_legacy_context_identity_is_shared_without_stm_fingerprint(self):
        class Session:
            pass

        session = Session()
        context = SimpleNamespace(
            request_context=SimpleNamespace(request=None), session=session
        )
        reporter = PresenceReporter()
        reporter.submit = Mock()
        reporter._send = Mock(return_value=True)
        reporter.report_context(context)
        reporter.report_state(context, "unknown")
        self.assertEqual(
            reporter.submit.call_args.args[0],
            reporter._send.call_args.args[0]["session_id"],
        )

    def test_non_http_context_cannot_borrow_validated_http_identity(self):
        class Session:
            pass

        reporter = PresenceReporter()
        request = SimpleNamespace(
            headers={"mcp-session-id": "borrowed-http-session"}, scope={}
        )
        context = SimpleNamespace(
            request_context=SimpleNamespace(request=request), session=Session()
        )
        fallback, owned = reporter.context_identity(context)
        self.assertFalse(owned)
        self.assertNotEqual(fallback, reporter.identity("borrowed-http-session"))
        self.assertEqual(fallback, reporter.context_identity(context)[0])

    def test_touch_does_not_override_known_state_or_clocks(self):
        clock = [0.0]
        registry = SessionStateRegistry(clock=lambda: clock[0])
        row = registry.presence({"provider": "mcp", "session_id": "client"})
        self.assertEqual(row["state"], "unknown")
        clock[0] = 1
        registry.upsert(
            {"provider": "mcp", "session_id": "client", "state": "needs_input"}
        )
        clock[0] = 2
        row = registry.presence({"provider": "mcp", "session_id": "client"})
        self.assertEqual(
            (row["state"], row["first_seen"], row["state_since"], row["last_seen"]),
            ("needs_input", 0, 1, 2),
        )
        registry.presence({"provider": "mcp", "session_id": "client", "ended": True})
        self.assertEqual(registry.snapshot(), [])

    def test_bubble_presence_never_duplicates_or_resurrects(self):
        registry = SessionStateRegistry()
        registry.claim(
            {
                "provider": "claude",
                "session_id": "owned",
                "state": "working",
                "is_bubble": True,
            }
        )
        payload = {"provider": "claude", "session_id": "owned", "bubble_owner": True}
        registry.presence(payload)
        registry.presence(dict(payload, ended=True))
        self.assertEqual(len(registry.snapshot()), 1)
        self.assertEqual(registry.snapshot()[0]["state"], "working")
        registry.remove("claude", "owned")
        self.assertIsNone(registry.presence(payload))
        self.assertEqual(registry.snapshot(), [])

    def test_validation_and_reporter_identity_are_metadata_only(self):
        self.assertIsNone(validate_presence({"provider": "mcp", "session_id": "x"})[1])
        for forbidden in ("state", "content", "tool_input", "path", "token"):
            self.assertIsNotNone(
                validate_presence(
                    {"provider": "mcp", "session_id": "x", forbidden: "secret"}
                )[1]
            )
        reporter = PresenceReporter()
        first = reporter.identity("transport-a")
        self.assertEqual(first, reporter.identity("transport-a"))
        self.assertNotEqual(first, reporter.identity("transport-b"))
        self.assertNotIn("transport", first)

    def test_queue_is_bounded_coalescing_and_nonblocking(self):
        reporter = PresenceReporter(capacity=3)
        reporter._thread = Mock()  # Inspect queued metadata without worker/network.
        for n in range(100):
            reporter.submit(f"{n:032d}")
        self.assertEqual(len(reporter._pending), 3)
        reporter.submit(f"{99:032d}", ended=True)
        self.assertEqual(len(reporter._pending), 3)
        self.assertTrue(list(reporter._pending.values())[-1]["ended"])
        self.assertEqual(
            set(list(reporter._pending.values())[-1]),
            {"provider", "session_id", "bubble_owner", "ended"},
        )

    def test_context_uses_transport_id_without_tool_arguments(self):
        reporter = PresenceReporter()
        reporter.submit = Mock()
        request = SimpleNamespace(
            headers={"mcp-session-id": "transport"},
            scope={"engram.presence.transport_id": "transport"},
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        reporter.report_context(context)
        reporter.submit.assert_called_once_with(reporter.identity("transport"))
        reporter.submit.reset_mock()
        request.headers["x-engram-bubble-owner"] = "a" * 32
        request.scope["engram.presence.local"] = True
        reporter.report_context(context)
        reporter.submit.assert_called_once_with("a" * 32, bubble_owner=True)

    def test_discovery_schema_loopback_and_token_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            reporter = PresenceReporter(path)
            reporter._opener = Mock()
            for info in (
                {
                    "schema_version": 2,
                    "host": "127.0.0.1",
                    "port": 123,
                    "token": "a" * 43,
                },
                {
                    "schema_version": 1,
                    "host": "example.com",
                    "port": 123,
                    "token": "a" * 43,
                },
            ):
                path.write_text(json.dumps(info))
                reporter._send({"provider": "mcp", "session_id": "x"})
            reporter._opener.open.assert_not_called()
            reporter._opener.open.return_value.__enter__ = Mock(return_value=Mock())
            reporter._opener.open.return_value.__exit__ = Mock(return_value=False)
            for token in ("a" * 43, "b" * 43):
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "host": "127.0.0.1",
                            "port": 123,
                            "token": token,
                        }
                    )
                )
                reporter._send({"provider": "mcp", "session_id": "x"})
                request = reporter._opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "Bearer " + token)

    def test_sdk_transport_log_does_not_include_handle(self):
        record = logging.LogRecord(
            "sdk", logging.INFO, "", 1, "Created session %s", ("a" * 32,), None
        )
        TransportIdentityFilter().filter(record)
        self.assertEqual(record.getMessage(), "Created session [session]")


class PresenceMiddlewareTests(unittest.TestCase):
    def test_principal_and_listener_guard_initialize_and_delete(self):
        async def exercise():
            reporter = Mock()
            reporter.identity.side_effect = lambda sid: "opaque-" + sid
            seen = []

            async def application(scope, receive, send):
                seen.append(scope)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"mcp-session-id", b"issued-id")],
                    }
                )
                await send({"type": "http.response.body", "body": b"{}"})

            app = PresenceMiddleware(application, reporter)

            async def request(
                port=17385, principal=None, sid=None, method="POST", bubble=None
            ):
                scope = {
                    "type": "http",
                    "path": "/mcp",
                    "method": method,
                    "server": ("127.0.0.1", port),
                    "headers": [],
                }
                if principal:
                    scope["engram.remote_principal"] = principal
                if sid:
                    scope["headers"].append((b"mcp-session-id", sid.encode()))
                if bubble:
                    scope["headers"].append((b"x-engram-bubble-owner", bubble.encode()))
                messages = []

                async def send(message):
                    messages.append(message)

                async def receive():
                    return {"type": "http.request", "body": b""}

                await app(scope, receive, send)
                return messages[0]["status"]

            self.assertEqual(await request(principal="A"), 200)
            self.assertEqual(await request(principal="B", sid="issued-id"), 403)
            self.assertEqual(await request(sid="issued-id"), 403)
            self.assertEqual(
                await request(port=17386, principal="A", sid="issued-id"), 403
            )
            self.assertEqual(await request(principal="A", sid="issued-id"), 200)
            self.assertEqual(seen[-1]["engram.presence.transport_id"], "issued-id")
            self.assertEqual(await request(principal="A", sid="missing"), 404)
            self.assertEqual(
                await request(
                    principal="A",
                    sid="issued-id",
                    method="DELETE",
                    bubble="forged-owner",
                ),
                200,
            )
            self.assertEqual(await request(principal="A", sid="issued-id"), 404)
            reporter.submit.assert_any_call("opaque-issued-id", ended=True)

        asyncio.run(exercise())


class BubblePresenceOptionsTests(unittest.TestCase):
    def test_invalid_optional_port_keeps_options_buildable(self):
        from overlay.bubble.session import BubbleSessionManager

        controller = SimpleNamespace(env_overrides={}, session_id="a" * 32, set_project=Mock())
        session = BubbleSessionManager(cwd=".", state_controller=controller)
        with patch(
            "overlay.config.load_cfg", return_value={"mcp": {"http_port": "invalid"}}
        ):
            self.assertEqual(session._build_options().mcp_servers, {})
        with patch(
            "overlay.config.load_cfg", return_value={"mcp": {"http_port": 17891}}
        ):
            entry = session._build_options().mcp_servers["engram"]
            self.assertEqual(entry["url"], "http://127.0.0.1:17891/mcp")
            self.assertEqual(entry["headers"], {"x-engram-bubble-owner": "a" * 32})


if __name__ == "__main__":
    unittest.main()
