"""engram_get_context_once(native_session_id=...) - Codex SessionStart hook 이 만든 세션과
나중의 MCP 연결이 같은 STM 세션으로 합류한다. DB 는 임시 디렉터리로 격리한다."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mcp_server
from core.storage.db import get_connection, initialize_db

NATIVE_A = "019a1111-1111-7111-8111-111111111111"
NATIVE_B = "019a2222-2222-7222-8222-222222222222"


def _connection(transport_id):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={}, scope={"engram.presence.transport_id": transport_id})
        )
    )


class ExplicitNativeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        self._db_patch = patch("core.storage.db._get_db_dir", return_value=self.db_dir)
        self._db_patch.start()
        initialize_db(self.db_dir)
        self._clear()

    def tearDown(self):
        self._clear()
        self._db_patch.stop()
        self._tmp.cleanup()

    def _clear(self):
        mcp_server._FINGERPRINT_TO_SESSION.clear()
        mcp_server._CONN_TO_NATIVE.clear()
        mcp_server._CONTEXT_ONCE_KEYS.clear()

    def session(self, native=""):
        conn = get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO sessions (scope_key, native_session_id) VALUES (?, ?)", ("overlay", native)
                )
            return int(cur.lastrowid)
        finally:
            conn.close()

    def native_of(self, sid):
        return mcp_server._session_native_id(sid)

    def once(self, ctx, start, **kw):
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_report_bootstrap_project", AsyncMock()
        ), patch.object(mcp_server, "_mark_trusted_root_bootstrap"), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server.memory_bus, "start_session", start), patch.object(
            mcp_server, "engram_get_context", AsyncMock(return_value="[ctx]")
        ):
            return asyncio.run(mcp_server.engram_get_context_once(
                caller="Codex", scope_key="overlay", ctx=ctx, **kw))

    def test_explicit_native_stamped_on_created_session(self):
        created = self.session()
        self.once(_connection("hook"), Mock(return_value=SimpleNamespace(session_id=created)),
                  native_session_id=NATIVE_A)
        self.assertEqual(self.native_of(created), NATIVE_A)

    def test_explicit_native_reuses_open_session_owning_it(self):
        existing = self.session(native=NATIVE_A)
        start = Mock()
        out = self.once(_connection("hook"), start, native_session_id=NATIVE_A)
        start.assert_not_called()
        self.assertIn(f"session_id={existing}", out)

    def test_later_model_connection_resolves_to_hook_created_session(self):
        created = self.session()
        self.once(_connection("hook"), Mock(return_value=SimpleNamespace(session_id=created)),
                  native_session_id=NATIVE_A)
        model = _connection("model")
        mcp_server._CONN_TO_NATIVE[mcp_server._context_once_connection_key(model)] = NATIVE_A
        self.assertEqual(mcp_server._resolve_connection_session(model), (created, "native"))

    def test_invalid_explicit_native_is_ignored(self):
        created = self.session()
        self.once(_connection("hook"), Mock(return_value=SimpleNamespace(session_id=created)),
                  native_session_id="bad id; DROP")
        self.assertEqual(self.native_of(created), "")

    def test_explicit_native_never_overwrites_a_different_native(self):
        created = self.session(native=NATIVE_B)
        self.once(_connection("hook"), Mock(return_value=SimpleNamespace(session_id=created)),
                  native_session_id=NATIVE_A)
        self.assertEqual(self.native_of(created), NATIVE_B)

    def test_connection_known_native_wins_over_explicit(self):
        ctx = _connection("conn")
        mcp_server._CONN_TO_NATIVE[mcp_server._context_once_connection_key(ctx)] = NATIVE_B
        created = self.session()
        self.once(ctx, Mock(return_value=SimpleNamespace(session_id=created)), native_session_id=NATIVE_A)
        self.assertEqual(self.native_of(created), NATIVE_B)

    def test_omitted_native_keeps_old_behavior(self):
        created = self.session()
        self.once(_connection("hook"), Mock(return_value=SimpleNamespace(session_id=created)))
        self.assertEqual(self.native_of(created), "")


if __name__ == "__main__":
    unittest.main()
