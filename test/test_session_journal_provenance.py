"""External journal authority is durable session provenance, never an HTTP hint."""

import json
import asyncio
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
from urllib.request import Request, urlopen

import mcp_server
from core.memory import store
from core.storage import db
from overlay.bubble.stm_bridge import StmBridge
from overlay.stm_server import _STMHandler


class SessionJournalProvenanceTests(unittest.TestCase):
    def test_only_trusted_bootstrap_callers_mark_external_journal_eligibility(self):
        with patch.object(mcp_server, "set_session_journal_provenance") as mark:
            mcp_server._mark_trusted_root_bootstrap(11, "Codex")
            mcp_server._mark_trusted_root_bootstrap(12, "claude-code")
            mcp_server._mark_trusted_root_bootstrap(13, "subagent")
            mcp_server._mark_trusted_root_bootstrap(14, "verification")
        self.assertEqual(mark.call_args_list[0].args, (11, "root_bootstrap"))
        self.assertEqual(mark.call_args_list[1].args, (12, "root_bootstrap"))
        self.assertEqual(len(mark.call_args_list), 2)

    def test_schema_provenance_is_not_backfilled_and_only_trusted_values_are_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = db.get_connection
            conn = raw(root)
            with conn:
                conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, ended_at TEXT, journal_provenance TEXT NOT NULL DEFAULT '')")
                conn.execute("INSERT INTO sessions(id) VALUES(1)")
                conn.execute("INSERT INTO sessions(id,journal_provenance) VALUES(2,'root_bootstrap')")
                conn.execute("INSERT INTO sessions(id,journal_provenance) VALUES(3,'internal')")
            conn.close()
            with patch.object(store, "get_connection", side_effect=lambda: raw(root)):
                self.assertFalse(store.session_has_external_journal_eligibility(1))
                self.assertTrue(store.session_has_external_journal_eligibility(2))
                self.assertFalse(store.session_has_external_journal_eligibility(3))
                self.assertFalse(store.set_session_journal_provenance(3, "internal"))

    def test_http_spoofed_origins_cannot_enable_external_directory(self):
        server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for label, origin in (("internal", "root"), ("subagent", "bubble"), ("verification", "")):
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/stm/session/summarize",
                    data=json.dumps({"session_id": 21, "scope_key": label, "journal_origin": origin}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with patch("overlay.stm_server._explicit_session_status", return_value="open"), patch(
                    "overlay.stm_server._resolve_open_session_id", return_value=21
                ), patch("overlay.stm_server.session_has_external_journal_eligibility", return_value=False), patch(
                    "overlay.stm_server.get_cfg_value", return_value="C:/must-not-be-used"
                ), patch("overlay.stm_server.checkpoint_open_session", return_value={"status": "checkpointed"}) as checkpoint:
                    with urlopen(request, timeout=2) as response:
                        self.assertEqual(json.loads(response.read().decode())["status"], "checkpointed")
                self.assertEqual(checkpoint.call_args.kwargs["external_daily_dir"], "", label)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_http_trusted_session_provenance_enables_configured_directory(self):
        server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/stm/session/summarize",
                data=json.dumps({"session_id": 22, "scope_key": "root"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with patch("overlay.stm_server._explicit_session_status", return_value="open"), patch(
                "overlay.stm_server._resolve_open_session_id", return_value=22
            ), patch("overlay.stm_server.session_has_external_journal_eligibility", return_value=True), patch(
                "overlay.stm_server.get_cfg_value", return_value="C:/configured-daily"
            ), patch("overlay.stm_server.checkpoint_open_session", return_value={"status": "checkpointed"}) as checkpoint:
                with urlopen(request, timeout=2):
                    pass
            self.assertEqual(checkpoint.call_args.kwargs["external_daily_dir"], "C:/configured-daily")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_bubble_start_is_the_trusted_bubble_provenance_path(self):
        session = type("Session", (), {"session_id": 31, "scope_key": "bubble"})()
        with patch("overlay.bubble.stm_bridge._memory_bus.start_session", return_value=session) as start:
            bridge = StmBridge(scope_key="bubble")
            bridge.open()
        start.assert_called_once_with(scope_key="bubble", journal_provenance="bubble")

    def test_mcp_direct_checkpoint_ignores_spoofed_root_origin(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"ended_at": None, "scope_key": "scope"}
        with patch.object(mcp_server, "resolve_scope_key", return_value="scope"), patch.object(
            mcp_server, "_unique_open_session_id", return_value=(41, False)
        ), patch.object(mcp_server, "get_connection", return_value=conn), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server, "session_has_external_journal_eligibility", return_value=False), patch.object(
            mcp_server, "checkpoint_open_session", return_value={"status": "checkpointed"}
        ) as checkpoint:
            result = asyncio.run(mcp_server.engram_summarize_session(summary="spoof", scope_key="scope", journal_origin="root"))
        self.assertEqual(result["status"], "checkpointed")
        self.assertEqual(checkpoint.call_args.kwargs["external_daily_dir"], "")


if __name__ == "__main__":
    unittest.main()
