"""MCP 세션 resolve 는 프로세스가 아니라 대화(연결) 단위다 - JJHbrams/Project_Engram#12.

배경: 연결 키가 `client:<id>|startup:<프로세스 토큰>` 하나로 뭉쳐 있어서, 마지막에
bootstrap 한 대화가 모든 연결의 세션이 됐다. summarize/close 가 다른 대화의 세션을
닫을 수 있었다. 이제 연결 -> native id(hook 보고) -> 열린 세션, 그다음 연결별
bootstrap 바인딩 순으로만 찾는다.

DB 는 임시 디렉터리로 격리한다(ENGRAM_DB_DIR 환경변수는 user.config.yaml 에 져서 격리가 아니다).
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mcp_server
from core.storage.db import get_connection, initialize_db

NATIVE_A = "11111111-1111-4111-8111-111111111111"
NATIVE_B = "22222222-2222-4222-8222-222222222222"


def _connection(transport_id):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={}, scope={"engram.presence.transport_id": transport_id})
        )
    )


class _DbCase(unittest.TestCase):
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

    def session(self, scope="overlay", native="", ended=False):
        conn = get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO sessions (scope_key, native_session_id, ended_at) VALUES (?, ?, ?)",
                    (scope, native, "2026-01-01 00:00:00" if ended else None),
                )
            return int(cur.lastrowid)
        finally:
            conn.close()

    def native_of(self, session_id):
        conn = get_connection()
        try:
            return conn.execute("SELECT native_session_id FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
        finally:
            conn.close()

    def is_open(self, session_id):
        conn = get_connection()
        try:
            return conn.execute("SELECT ended_at FROM sessions WHERE id=?", (session_id,)).fetchone()[0] is None
        finally:
            conn.close()

    def count(self):
        conn = get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()

    def key(self, ctx):
        return mcp_server._context_once_connection_key(ctx)

    def report(self, ctx, native, event="UserPromptSubmit", agent_id=None, result=None):
        mcp_server._remember_connection_native(
            ctx, event, agent_id, native, result if result is not None else {"accepted": True, "reason": "delivered"}
        )


class HookEventsRememberConnectionNativeTests(_DbCase):
    def test_accepted_root_event_is_remembered(self):
        self.report(_connection("conn-a"), NATIVE_A)
        self.assertEqual(list(mcp_server._CONN_TO_NATIVE.values()), [NATIVE_A])

    def test_rejections_subagents_and_empty_ids_are_not_remembered(self):
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A, result={"accepted": False, "reason": "native_identity_conflict"})
        self.report(ctx, NATIVE_A, event="SubagentStart")
        self.report(ctx, NATIVE_A, agent_id="agent-1")
        self.report(ctx, "")
        self.report(ctx, None)
        self.assertEqual(len(mcp_server._CONN_TO_NATIVE), 0)

    def test_overlay_unavailable_still_proves_identity(self):
        self.report(_connection("conn-a"), NATIVE_A, result={"accepted": False, "reason": "overlay_unavailable"})
        self.assertEqual(len(mcp_server._CONN_TO_NATIVE), 1)

    def test_claude_tool_records_only_after_presence_accepts(self):
        ctx = _connection("conn-a")
        for presence_result, expected in (
            ({"accepted": False, "reason": "native_identity_conflict"}, 0),
            ({"accepted": True, "reason": "delivered"}, 1),
        ):
            self._clear()
            with patch.object(mcp_server.engramMCP, "get_context", return_value=ctx), patch.object(
                mcp_server._mcp_presence, "report_claude_event", return_value=presence_result
            ):
                out = asyncio.run(mcp_server.engram_report_claude_event(
                    event="UserPromptSubmit", turn_id="t", native_session_id=NATIVE_A))
            self.assertEqual(len(mcp_server._CONN_TO_NATIVE), expected)
            self.assertNotIn(NATIVE_A, str(out))  # 원문 id 는 응답에도 나가지 않는다

    def test_codex_tool_records_root_event(self):
        ctx = _connection("conn-codex")
        with patch.object(mcp_server.engramMCP, "get_context", return_value=ctx), patch.object(
            mcp_server._mcp_presence, "report_codex_event", return_value={"accepted": True, "reason": "delivered"}
        ):
            asyncio.run(mcp_server.engram_report_codex_event(
                event="UserPromptSubmit", turn_id="t", native_session_id=NATIVE_B))
        self.assertEqual(list(mcp_server._CONN_TO_NATIVE.values()), [NATIVE_B])

    def test_maps_are_bounded(self):
        with patch.object(mcp_server, "_CONNECTION_MAP_MAX", 3):
            for i in range(6):
                self.report(_connection(f"conn-{i}"), NATIVE_A)
                mcp_server._bind_connection_session(f"conn:{i}", 100 + i)
        self.assertEqual(len(mcp_server._CONN_TO_NATIVE), 3)
        self.assertEqual(len(mcp_server._FINGERPRINT_TO_SESSION), 3)


class ResolverPerConversationTests(_DbCase):
    def test_two_connections_resolve_their_own_sessions(self):
        a, b = self.session(native=NATIVE_A), self.session(native=NATIVE_B)
        ca, cb = _connection("conn-a"), _connection("conn-b")
        self.report(ca, NATIVE_A)
        self.report(cb, NATIVE_B)
        self.assertEqual(mcp_server._resolve_connection_session(ca), (a, "native"))
        self.assertEqual(mcp_server._resolve_connection_session(cb), (b, "native"))

    def test_new_connection_with_same_native_id_resolves_the_original_session(self):
        original = self.session(native=NATIVE_A)
        self.report(_connection("before-reconnect"), NATIVE_A)
        mcp_server._CONN_TO_NATIVE.clear()  # 재연결/재시작: 메모리는 비고 연결 식별자는 바뀐다
        after = _connection("after-reconnect")
        self.assertEqual(mcp_server._resolve_connection_session(after), (0, ""))
        self.report(after, NATIVE_A)
        self.assertEqual(mcp_server._resolve_connection_session(after), (original, "native"))

    def test_explicit_id_wins_and_lookup_never_creates(self):
        self.report(_connection("conn-a"), NATIVE_A)
        before = self.count()
        self.assertEqual(mcp_server._resolve_connection_session(_connection("conn-a"), 77), (77, "explicit"))
        self.assertEqual(mcp_server._resolve_connection_session(_connection("conn-a")), (0, ""))
        self.assertEqual(self.count(), before)

    def test_binding_is_per_connection_not_process_wide(self):
        s1 = self.session()
        a, b = _connection("conn-a"), _connection("conn-b")
        mcp_server._bind_connection_session(self.key(a), s1)
        self.assertEqual(mcp_server._resolve_connection_session(a), (s1, "binding"))
        self.assertEqual(mcp_server._resolve_connection_session(b), (0, ""))

    def test_ended_native_session_falls_back_to_connection_binding(self):
        self.session(native=NATIVE_A, ended=True)
        bound = self.session()
        ctx = _connection("conn-a")
        mcp_server._bind_connection_session(self.key(ctx), bound)
        self.report(ctx, NATIVE_A)
        # 끝난 native 세션은 소유자가 아니므로 bootstrap 세션이 native id 를 이어받고 같은 세션으로 해석된다.
        self.assertEqual(mcp_server._resolve_connection_session(ctx)[0], bound)


class NativeAttachTests(_DbCase):
    def test_first_hook_event_attaches_native_to_bootstrap_session(self):
        boot = self.session()
        ctx = _connection("conn-a")
        mcp_server._bind_connection_session(self.key(ctx), boot)
        self.report(ctx, NATIVE_A)
        self.assertEqual(self.native_of(boot), NATIVE_A)

    def test_attach_refuses_when_another_open_session_owns_the_native_id(self):
        owner = self.session(native=NATIVE_A)
        boot = self.session()
        ctx = _connection("conn-a")
        mcp_server._bind_connection_session(self.key(ctx), boot)
        self.report(ctx, NATIVE_A)
        self.assertEqual(self.native_of(boot), "")
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (owner, "native"))

    def test_attach_never_overwrites_a_different_native_id(self):
        boot = self.session(native=NATIVE_B)
        self.assertFalse(mcp_server._attach_native_to_session(boot, NATIVE_A))
        self.assertEqual(self.native_of(boot), NATIVE_B)

    def _once(self, ctx, start_session):
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_report_bootstrap_project", AsyncMock()
        ), patch.object(mcp_server, "_mark_trusted_root_bootstrap"), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server.memory_bus, "start_session", start_session), patch.object(
            mcp_server, "engram_get_context", AsyncMock(return_value="[ctx]")
        ):
            return asyncio.run(mcp_server.engram_get_context_once(
                caller="claude-code", scope_key="overlay", ctx=ctx))

    def test_bootstrap_reuses_open_session_that_already_has_the_native_id(self):
        # transcript 캡처가 먼저 세션을 만든 대화: bootstrap 이 두 번째 세션을 만들면 안 된다.
        existing = self.session(native=NATIVE_A)
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        start = Mock()
        out = self._once(ctx, start)
        start.assert_not_called()
        self.assertIn(f"session_id={existing}", out)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (existing, "native"))

    def test_bootstrap_stamps_native_id_on_the_session_it_creates(self):
        created = self.session()
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)  # 아직 열린 native 세션이 없다 -> 새로 만들고 id 를 물린다
        self._once(ctx, Mock(return_value=SimpleNamespace(session_id=created)))
        self.assertEqual(self.native_of(created), NATIVE_A)

    def test_bootstrap_without_native_binds_only_its_own_connection(self):
        s1 = self.session()
        a, b = _connection("conn-a"), _connection("conn-b")
        self._once(a, Mock(return_value=SimpleNamespace(session_id=s1)))
        self.assertEqual(mcp_server._resolve_connection_session(a), (s1, "binding"))
        self.assertEqual(mcp_server._resolve_connection_session(b), (0, ""))


class ToolsNeverCrossConversationsTests(_DbCase):
    def _summarize(self, ctx, **kw):
        checkpoint = Mock(return_value={"status": "checkpointed"})
        with patch.object(mcp_server, "_stm_post", return_value=None), patch.object(
            mcp_server, "checkpoint_open_session", checkpoint
        ), patch.object(mcp_server, "session_has_external_journal_eligibility", return_value=False):
            out = asyncio.run(mcp_server.engram_summarize_session(
                summary="s", scope_key="overlay", cwd="C:/x", ctx=ctx, **kw))
        return out, checkpoint

    def test_summarize_targets_the_calling_conversations_session(self):
        a, b = self.session(native=NATIVE_A), self.session(native=NATIVE_B)
        ca, cb = _connection("conn-a"), _connection("conn-b")
        self.report(ca, NATIVE_A)
        self.report(cb, NATIVE_B)
        _, cp_a = self._summarize(ca)
        _, cp_b = self._summarize(cb)
        self.assertEqual(cp_a.call_args.args[0], a)
        self.assertEqual(cp_b.call_args.args[0], b)

    def test_unknown_connection_never_picks_another_connections_session(self):
        a = self.session(native=NATIVE_A)
        self.session(native=NATIVE_B)  # 같은 scope 에 열린 세션이 둘 -> 모호
        self.report(_connection("conn-a"), NATIVE_A)
        stranger = _connection("conn-stranger")
        out, checkpoint = self._summarize(stranger)
        self.assertEqual(out["status"], "ambiguous_open_session")
        checkpoint.assert_not_called()
        self.assertEqual(mcp_server._resolve_connection_session(stranger), (0, ""))
        closed = asyncio.run(mcp_server.engram_close_session(
            summary="s", scope_key="overlay", cwd="C:/x", ctx=stranger))
        self.assertEqual(closed["status"], "ambiguous_open_session")
        self.assertFalse(closed["session_closed"])
        self.assertTrue(self.is_open(a))

    def test_close_without_session_id_closes_only_this_conversations_session(self):
        a, b = self.session(native=NATIVE_A), self.session(native=NATIVE_B)
        ca = _connection("conn-a")
        self.report(ca, NATIVE_A)
        with patch.object(mcp_server, "engram_summarize_session",
                          AsyncMock(return_value={"status": "checkpointed"})) as summarize, patch.object(
            mcp_server, "_close_scoped_session", Mock(side_effect=lambda scope, s, sid, *rest: sid)
        ) as closer, patch.object(mcp_server, "_schedule_post_session_sync", return_value=None), patch.object(
            mcp_server, "mark_session_continuity_saved"
        ):
            out = asyncio.run(mcp_server.engram_close_session(
                summary="s", scope_key="overlay", cwd="C:/x", ctx=ca))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(summarize.call_args.kwargs["session_id"], a)
        self.assertEqual(int(closer.call_args.args[2]), a)
        self.assertNotEqual(a, b)

    def test_save_message_uses_native_session_and_never_borrows_another(self):
        a = self.session(native=NATIVE_A)
        ca, cb = _connection("conn-a"), _connection("conn-b")
        self.report(ca, NATIVE_A)
        with patch.object(mcp_server, "_stm_post", return_value={"status": "ok"}) as post:
            mcp_server.engram_save_message(role="user", content="hi", ctx=ca)
            self.assertEqual(post.call_args.args[1]["session_id"], a)
            out = mcp_server.engram_save_message(role="user", content="hi", ctx=cb)
        self.assertEqual(out["status"], "error")

    def test_save_message_may_create_session_for_known_native_id(self):
        ca = _connection("conn-a")
        self.report(ca, NATIVE_A)
        with patch.object(mcp_server, "_stm_post", return_value={"status": "ok"}) as post:
            mcp_server.engram_save_message(role="user", content="hi", scope_key="overlay", ctx=ca)
        sid = post.call_args.args[1]["session_id"]
        self.assertEqual(self.native_of(sid), NATIVE_A)

    def test_peek_and_transcript_scope_follow_the_connection(self):
        self.session(scope="project:other", native=NATIVE_B)
        self.session(scope="project:mine", native=NATIVE_A)
        ca = _connection("conn-a")
        self.report(ca, NATIVE_A)
        self.assertEqual(mcp_server._transcript_scope("", "", ca), "project:mine")
        with patch.object(mcp_server, "_stm_get", return_value={"messages": []}):
            out = asyncio.run(mcp_server.engram_peek_stm(ctx=ca))
        self.assertEqual(out["scope_key"], "project:mine")
        stranger_scope = mcp_server._transcript_scope("", "C:/nowhere", _connection("conn-x"))
        self.assertNotEqual(stranger_scope, "project:mine")


class ScopeMismatchStatusTests(_DbCase):
    """open 인데 scope 만 다른 세션을 ended_session 이라 부르면 안 된다(#12 C)."""

    def _summarize(self, **kw):
        with patch.object(mcp_server, "_stm_post", return_value=None), patch.object(
            mcp_server, "resolve_scope_key", return_value="project:cwd-derived"
        ):
            return asyncio.run(mcp_server.engram_summarize_session(summary="s", cwd="C:/x", **kw))

    def test_summarize_open_session_with_other_scope_is_scope_mismatch(self):
        sid = self.session(scope="overlay")
        out = self._summarize(session_id=sid)
        self.assertEqual(out, {"status": "scope_mismatch", "session_scope_key": "overlay",
                               "scope_key": "project:cwd-derived"})

    def test_summarize_ended_only_when_ended_at_is_set(self):
        sid = self.session(scope="overlay", ended=True)
        self.assertEqual(self._summarize(session_id=sid)["status"], "ended_session")

    def test_summarize_missing_row_is_unknown_session(self):
        self.assertEqual(self._summarize(session_id=9999)["status"], "unknown_session")

    def _close(self, **kw):
        with patch.object(mcp_server, "resolve_scope_key", return_value="project:cwd-derived"), patch.object(
            mcp_server, "_salvage_working_memory", return_value={"saved": True}
        ):
            return asyncio.run(mcp_server.engram_close_session(summary="s", cwd="C:/x", **kw))

    def test_close_open_session_with_other_scope_is_scope_mismatch_and_salvages(self):
        sid = self.session(scope="overlay")
        out = self._close(session_id=sid)
        self.assertEqual(out["status"], "scope_mismatch")
        self.assertEqual(out["session_scope_key"], "overlay")
        self.assertEqual(out["scope_key"], "project:cwd-derived")
        self.assertFalse(out["session_closed"])
        self.assertEqual(out["salvaged_working_memory"], {"saved": True})
        self.assertTrue(self.is_open(sid))

    def test_close_ended_and_unknown_sessions(self):
        ended = self.session(scope="overlay", ended=True)
        self.assertEqual(self._close(session_id=ended)["status"], "ended_session")
        self.assertEqual(self._close(session_id=9999)["status"], "unknown_session")


def _derived_scope(explicit=None, **_kw):
    # 실제 resolve_scope_key 처럼 명시 scope 는 그대로, 없으면 cwd 에서 project:* 를 유도한다.
    return explicit or "project:cwd-derived"


class ImplicitScopeAdoptionTests(_DbCase):
    """연결에서 찾은 세션은 호출자가 scope 를 안 줬으면 자기 scope 를 따른다(cwd 유도 scope 와 어긋나도)."""

    def _ctx(self):
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        return ctx

    def test_summarize_without_scope_key_hits_own_overlay_session(self):
        sid = self.session(scope="overlay", native=NATIVE_A)
        ctx = self._ctx()
        checkpoint = Mock(return_value={"status": "checkpointed"})
        with patch.object(mcp_server, "resolve_scope_key", side_effect=_derived_scope), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server, "checkpoint_open_session", checkpoint), patch.object(
            mcp_server, "session_has_external_journal_eligibility", return_value=False
        ):
            out = asyncio.run(mcp_server.engram_summarize_session(summary="s", cwd="C:/x", ctx=ctx))
        self.assertEqual(out["status"], "checkpointed")
        self.assertEqual(checkpoint.call_args.args[:2], (sid, "overlay"))

    def test_close_without_scope_key_closes_own_overlay_session_with_adopted_scope(self):
        sid = self.session(scope="overlay", native=NATIVE_A)
        ctx = self._ctx()
        checkpoint = Mock(return_value={"status": "checkpointed"})
        closer = Mock(side_effect=lambda scope, s, session, *rest: session)
        with patch.object(mcp_server, "resolve_scope_key", side_effect=_derived_scope), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server, "checkpoint_open_session", checkpoint), patch.object(
            mcp_server, "session_has_external_journal_eligibility", return_value=False
        ), patch.object(mcp_server, "_close_scoped_session", closer), patch.object(
            mcp_server, "_schedule_post_session_sync", return_value=None
        ), patch.object(mcp_server, "mark_session_continuity_saved"):
            out = asyncio.run(mcp_server.engram_close_session(summary="s", cwd="C:/x", ctx=ctx))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["scope_key"], "overlay")
        self.assertEqual(checkpoint.call_args.args[:2], (sid, "overlay"))
        self.assertEqual(closer.call_args.args[0], "overlay")
        self.assertEqual(int(closer.call_args.args[2]), sid)

    def test_explicit_session_id_or_scope_key_keeps_scope_mismatch(self):
        sid = self.session(scope="overlay", native=NATIVE_A)
        ctx = self._ctx()
        with patch.object(mcp_server, "resolve_scope_key", side_effect=_derived_scope), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server, "_salvage_working_memory", return_value={}):
            by_id = asyncio.run(mcp_server.engram_summarize_session(
                summary="s", cwd="C:/x", session_id=sid, ctx=ctx))
            closed = asyncio.run(mcp_server.engram_close_session(
                summary="s", cwd="C:/x", scope_key="project:cwd-derived", ctx=ctx))
        self.assertEqual(by_id["status"], "scope_mismatch")
        self.assertEqual(closed["status"], "scope_mismatch")
        self.assertTrue(self.is_open(sid))


class NativeIdChangeOnSameConnectionTests(_DbCase):
    """/clear 나 새 대화로 같은 MCP 연결의 native id 가 바뀌면 이전 대화의 세션을 쓰면 안 된다."""

    def conflict(self, ctx, native):
        self.report(ctx, native, result={"accepted": False, "reason": "native_identity_conflict"})

    def test_conflict_with_different_id_drops_connection_memory(self):
        a = self.session(native=NATIVE_A)
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        mcp_server._bind_connection_session(self.key(ctx), a)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (a, "native"))
        self.conflict(ctx, NATIVE_B)
        self.assertEqual(len(mcp_server._CONN_TO_NATIVE), 0)
        self.assertEqual(len(mcp_server._FINGERPRINT_TO_SESSION), 0)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (0, ""))

    def test_conflict_with_the_same_id_keeps_memory(self):
        a = self.session(native=NATIVE_A)
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        self.conflict(ctx, NATIVE_A)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (a, "native"))

    def test_bound_session_with_a_different_native_id_is_never_returned(self):
        a = self.session(native=NATIVE_A)
        ctx = _connection("conn-b")
        mcp_server._bind_connection_session(self.key(ctx), a)
        self.report(ctx, NATIVE_B)  # 연결의 native 는 B, 바인딩 세션은 A 의 것
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (0, ""))

    def test_bound_session_with_native_id_is_not_returned_when_connection_native_is_unknown(self):
        a = self.session(native=NATIVE_A)
        ctx = _connection("conn-b")
        mcp_server._bind_connection_session(self.key(ctx), a)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (0, ""))

    def test_summarize_after_native_change_never_touches_the_old_conversation(self):
        a = self.session(native=NATIVE_A)
        self.session(native=NATIVE_B)  # 같은 scope 에 열린 세션 둘 -> scope fallback 도 모호
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        self.conflict(ctx, NATIVE_B)
        checkpoint = Mock(return_value={"status": "checkpointed"})
        with patch.object(mcp_server, "_stm_post", return_value=None), patch.object(
            mcp_server, "checkpoint_open_session", checkpoint
        ):
            out = asyncio.run(mcp_server.engram_summarize_session(
                summary="s", scope_key="overlay", cwd="C:/x", ctx=ctx))
        self.assertEqual(out["status"], "ambiguous_open_session")
        checkpoint.assert_not_called()
        self.assertTrue(self.is_open(a))

    def test_get_context_once_after_native_change_returns_full_context_and_new_session(self):
        old = self.session(native=NATIVE_A)
        ctx = _connection("conn-a")
        self.report(ctx, NATIVE_A)
        fresh = self.session()
        ids = iter([fresh])
        start = Mock(side_effect=lambda **_kw: SimpleNamespace(session_id=next(ids)))

        def once():
            with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
                mcp_server, "_report_bootstrap_project", AsyncMock()
            ), patch.object(mcp_server, "_mark_trusted_root_bootstrap"), patch.object(
                mcp_server, "_stm_post", return_value=None
            ), patch.object(mcp_server.memory_bus, "start_session", start), patch.object(
                mcp_server, "engram_get_context", AsyncMock(return_value="[persona]")
            ):
                return asyncio.run(mcp_server.engram_get_context_once(
                    caller="claude-code", scope_key="overlay", ctx=ctx))

        first = once()
        self.assertIn(f"session_id={old}", first)
        self.assertIn("already initialized", once())  # 같은 대화 재호출은 여전히 dedupe
        self.conflict(ctx, NATIVE_B)  # /clear: 같은 연결, 다른 native id
        again = once()
        self.assertNotIn("already initialized", again)
        self.assertIn("[persona]", again)
        self.assertIn(f"session_id={fresh}", again)
        self.assertNotIn(f"session_id={old}", again)
        self.assertEqual(mcp_server._resolve_connection_session(ctx), (fresh, "binding"))

    def test_save_message_create_inherits_known_connection_scope(self):
        other = self.session(scope="project:mine", native=NATIVE_B)
        ctx = _connection("conn-a")
        mcp_server._bind_connection_session(self.key(ctx), other)
        self.report(ctx, NATIVE_A)
        with patch.object(mcp_server, "_stm_post", return_value={"status": "ok"}) as post:
            mcp_server.engram_save_message(role="user", content="hi", ctx=ctx)
        sid = post.call_args.args[1]["session_id"]
        self.assertNotEqual(sid, other)
        self.assertEqual(self.native_of(sid), NATIVE_A)
        conn = get_connection()
        try:
            scope = conn.execute("SELECT scope_key FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(scope, "project:mine")


if __name__ == "__main__":
    unittest.main()
