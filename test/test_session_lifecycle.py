import unittest
import asyncio
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import mcp_server
from overlay.stm_server import _resolve_open_session_id
from core.memory import store
from core.graph.semantic import stm_promoter
from core.storage import db


class SessionLifecycleTests(unittest.TestCase):
    @patch("overlay.stm_server.get_connection")
    def test_unqualified_scope_refuses_two_open_sessions(self, get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [[273], [274]]
        get_connection.return_value = conn
        self.assertIsNone(_resolve_open_session_id(None, "overlay"))

    @patch.object(store, "get_connection")
    def test_ended_session_write_is_rejected(self, get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        get_connection.return_value = conn
        with self.assertRaisesRegex(ValueError, "not open"):
            store.save_message(274, "user", "must not write")

    def test_bubble_has_no_post_close_working_memory_or_promotion(self):
        from pathlib import Path
        source = Path("overlay/bubble/stm_bridge.py").read_text(encoding="utf-8")
        close_body = source[source.index("def close("):]
        self.assertNotIn("update_working_memory_from_recent_session", close_body)
        self.assertNotIn("maybe_promote(", close_body)

    def test_rebind_continuation_replaces_client_and_context_cache(self):
        session = MagicMock(session_id=275)
        with patch.object(mcp_server.memory_bus, "start_session", return_value=session) as start:
            mcp_server._CONTEXT_ONCE_KEYS.clear()
            mcp_server._FINGERPRINT_TO_SESSION.clear()
            sid = mcp_server._rebind_continuation("client:two", 274, "overlay", "cache-key")
        self.assertEqual(sid, 275)
        start.assert_called_once_with(scope_key="overlay", continued_from_session_id=274)
        self.assertEqual(mcp_server._FINGERPRINT_TO_SESSION["client:two"], 275)
        self.assertEqual(mcp_server._CONTEXT_ONCE_KEYS["cache-key"][0], 275)

    @patch.object(mcp_server, "_session_is_ended", return_value=True)
    @patch.object(mcp_server, "_stm_post", return_value=None)
    @patch("core.memory.close_session")
    def test_close_direct_fallback_closes_exact_session(self, close_session, _stm_post, _ended):
        # The helper imports close_session at call time, so direct mode must not
        # fall through merely because the broker is unavailable.
        self.assertEqual(mcp_server._close_scoped_session("overlay", "done", "321"), "321")
        close_session.assert_called_once()

    def test_durable_claim_allows_only_one_cross_connection_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = db.get_connection
            conn = raw(root)
            with conn:
                conn.execute("CREATE TABLE session_checkpoint_claims (session_id INTEGER,last_message_id INTEGER,claim_id TEXT,status TEXT,claimed_at REAL,PRIMARY KEY(session_id,last_message_id))")
            conn.close()
            results = []
            gate = threading.Barrier(2)
            def claim():
                gate.wait()
                results.append(stm_promoter._claim_checkpoint(9, 20)[0])
            with patch.object(stm_promoter, "get_connection", side_effect=lambda: raw(root)):
                threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
            self.assertEqual(results.count("acquired"), 1)
            self.assertEqual(results.count("busy"), 1)

    def test_failed_claim_is_released_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, raw = Path(tmp), db.get_connection
            conn = raw(root)
            with conn:
                conn.execute("CREATE TABLE session_checkpoint_claims (session_id INTEGER,last_message_id INTEGER,claim_id TEXT,status TEXT,claimed_at REAL,PRIMARY KEY(session_id,last_message_id))")
            conn.close()
            with patch.object(stm_promoter, "get_connection", side_effect=lambda: raw(root)):
                state, claim_id = stm_promoter._claim_checkpoint(9, 21)
                self.assertEqual(state, "acquired")
                stm_promoter._finish_checkpoint_claim(9, 21, claim_id, "failed")
                self.assertEqual(stm_promoter._claim_checkpoint(9, 21)[0], "acquired")

    def test_root_token_scopes_context_cache(self):
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        first, second = MagicMock(session_id=501), MagicMock(session_id=502)
        async def context(**_kwargs): return "context"
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_root_client_token_active", return_value=True
        ), patch.object(mcp_server, "_bind_root_client_token", return_value=True), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(
            mcp_server, "_session_is_open", return_value=True
        ), patch.object(mcp_server.memory_bus, "start_session", side_effect=[first, second]) as start, patch.object(
            mcp_server, "engram_get_context", side_effect=context
        ):
            asyncio.run(mcp_server.engram_get_context_once(scope_key="overlay", client_token="one"))
            asyncio.run(mcp_server.engram_get_context_once(scope_key="overlay", client_token="two"))
        self.assertEqual(start.call_count, 2)

    def test_same_root_token_reuses_context_cache(self):
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        session = MagicMock(session_id=503)

        async def context(**_kwargs):
            return "context"

        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_root_client_token_active", return_value=True
        ), patch.object(mcp_server, "_bind_root_client_token", return_value=True), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server, "_session_is_open", return_value=True), patch.object(
            mcp_server.memory_bus, "start_session", return_value=session
        ) as start, patch.object(mcp_server, "engram_get_context", side_effect=context):
            asyncio.run(mcp_server.engram_get_context_once(scope_key="overlay", client_token="same"))
            result = asyncio.run(mcp_server.engram_get_context_once(scope_key="overlay", client_token="same"))

        self.assertEqual(start.call_count, 1)
        self.assertIn("session_id=503", result)

    def test_invalid_root_token_does_not_create_session(self):
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_root_client_token_active", return_value=False
        ), patch.object(mcp_server.memory_bus, "start_session") as start, patch.object(
            mcp_server, "_stm_post"
        ) as post:
            result = asyncio.run(mcp_server.engram_get_context_once(scope_key="overlay", client_token="invalid"))

        self.assertEqual(result, "[engram] invalid root client token.")
        start.assert_not_called()
        post.assert_not_called()

    def test_root_token_cannot_rebind_another_owner_session(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"root_client_token": "original"}
        with patch.object(mcp_server, "get_connection", return_value=conn):
            self.assertFalse(mcp_server._bind_root_client_token(504, "different"))

        self.assertEqual(conn.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
