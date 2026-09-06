"""Runtime-shaped tests for first-provision audit correlation."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server


class RemoteProvisionProofTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_nonce_is_attached_to_exact_tools_list_audit_event(self):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode("utf-8")
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0)

        async def send(_message):
            return None

        async def downstream(_scope, app_receive, _send):
            await app_receive()

        principal = SimpleNamespace(
            name="remote-default", scope=None, denies=lambda _name: False
        )
        middleware = mcp_server.RemoteGuardMiddleware(downstream, remote_port=17386)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "server": ("127.0.0.1", 17386),
            "headers": [
                (b"authorization", b"Bearer test-token"),
                (b"x-engram-provision-proof", b"proof_nonce_123456"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }

        with patch.object(
            mcp_server, "_REMOTE_PRINCIPALS", {"test-token": principal}
        ), patch.object(mcp_server._call_log, "audit_remote") as audit:
            await middleware(scope, receive, send)

        audit.assert_called_once_with(
            principal="remote-default",
            action="allow",
            tool="tools/list",
            path="/mcp",
            detail="provision_nonce=proof_nonce_123456",
        )

    async def test_invalid_nonce_is_not_written_to_audit_detail(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0)

        async def send(_message):
            return None

        async def downstream(_scope, app_receive, _send):
            await app_receive()

        principal = SimpleNamespace(name="p", scope=None, denies=lambda _name: False)
        middleware = mcp_server.RemoteGuardMiddleware(downstream, remote_port=17386)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "server": ("127.0.0.1", 17386),
            "headers": [
                (b"authorization", b"Bearer t"),
                (b"x-engram-provision-proof", b"bad value with spaces"),
            ],
        }
        with patch.object(mcp_server, "_REMOTE_PRINCIPALS", {"t": principal}), \
             patch.object(mcp_server._call_log, "audit_remote") as audit:
            await middleware(scope, receive, send)

        self.assertEqual(audit.call_args.kwargs["detail"], "")


if __name__ == "__main__":
    unittest.main()
