"""context-once 캐시 키 회귀.

배경: 전역 ~/.claude/CLAUDE.md 는 고정 cwd 를, SessionStart hook 은 프로젝트별
cwd 를 지시한다. 둘 다 같은 scope_key='overlay' 로 bootstrap 하는데 cwd 가
캐시 키에 들어 있어서 매 세션 bootstrap 이 두 번 실행되고 STM 세션이 하나 더
생겼다(2026-09-17 실측: 하루 5개 중 2개가 이 원인).

scope_key 가 명시되면 resolve_scope_key 는 cwd 를 보지 않으므로, 두 호출은
같은 컨텍스트를 받는다. 그러면 캐시도 갈리면 안 된다.
"""

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mcp_server
from mcp_server import _build_context_once_key
from core.identity import personality


CLAUDE_MD_CWD = "C:/Users/jhjang/vault623/workspace/projects"
HOOK_CWD = r"C:\Users\jhjang\vault623\workspace\projects\ProjectIntelContunuum"


class ExplicitScopeIgnoresCwdTests(unittest.TestCase):
    def test_same_scope_different_cwd_shares_one_key(self):
        a = _build_context_once_key("claude-code", "overlay", "", CLAUDE_MD_CWD)
        b = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD)
        self.assertEqual(a, b)

    def test_empty_cwd_also_shares_the_key(self):
        a = _build_context_once_key("claude-code", "overlay", "", "")
        b = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD)
        self.assertEqual(a, b)

    def test_different_scope_still_separates(self):
        a = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD)
        b = _build_context_once_key("claude-code", "project:other", "", HOOK_CWD)
        self.assertNotEqual(a, b)

    def test_different_caller_still_separates(self):
        a = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD)
        b = _build_context_once_key("codex", "overlay", "", HOOK_CWD)
        self.assertNotEqual(a, b)

    def test_fingerprint_still_separates(self):
        a = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD, session_fingerprint="fp-1")
        b = _build_context_once_key("claude-code", "overlay", "", HOOK_CWD, session_fingerprint="fp-2")
        self.assertNotEqual(a, b)


class ImplicitScopeStillUsesCwdTests(unittest.TestCase):
    """scope_key 가 없으면 cwd 가 scope 를 유도하므로 키에 남아야 한다."""

    def test_no_scope_key_keeps_cwd_in_key(self):
        a = _build_context_once_key("claude-code", "", "", "C:/work/project-a")
        b = _build_context_once_key("claude-code", "", "", "C:/work/project-b")
        self.assertNotEqual(a, b)

    def test_no_scope_key_normalizes_separators(self):
        a = _build_context_once_key("claude-code", "", "", "C:/work/proj")
        b = _build_context_once_key("claude-code", "", "", r"C:\work\proj")
        self.assertEqual(a, b)


class DirectContextSessionTests(unittest.TestCase):
    def tearDown(self):
        mcp_server._FINGERPRINT_TO_SESSION.clear()

    def test_direct_context_recalculates_with_only_a_resolved_session_key(self):
        mcp_server._FINGERPRINT_TO_SESSION["client:known"] = 77
        compose = AsyncMock(return_value="context")
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_context_session_fingerprint", return_value="client:known"
        ), patch.object(mcp_server, "_session_is_open", return_value=True), patch.object(
            mcp_server.memory_bus, "compose_prompt_context", compose
        ), patch.object(mcp_server, "get_identity", return_value={"name": "name"}), patch.object(
            mcp_server, "get_persona_status", return_value={"initialized": True}
        ), patch.object(mcp_server, "_render_tutorial_notice", return_value=""):
            asyncio.run(mcp_server.engram_get_context(user_query="ordinary request", ctx=object()))
            asyncio.run(mcp_server.engram_get_context(user_query="safety request", ctx=object()))
        self.assertEqual(compose.await_count, 2)
        self.assertEqual(compose.await_args_list[0].kwargs["session_key"], 77)

    def test_unknown_direct_context_is_stateless(self):
        compose = AsyncMock(return_value="context")
        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_context_session_fingerprint", return_value="client:unknown"
        ), patch.object(mcp_server.memory_bus, "compose_prompt_context", compose), patch.object(
            mcp_server, "get_identity", return_value={"name": "name"}
        ), patch.object(mcp_server, "get_persona_status", return_value={"initialized": True}), patch.object(
            mcp_server, "_render_tutorial_notice", return_value=""
        ):
            asyncio.run(mcp_server.engram_get_context(user_query="ordinary request", ctx=object()))
        self.assertIsNone(compose.await_args.kwargs["session_key"])


class ContextOnceCadenceCleanupTests(unittest.TestCase):
    def tearDown(self):
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        personality.clear_situational_humor_state(711)

    def test_ttl_eviction_eagerly_clears_cached_session_cadence(self):
        cache_key = _build_context_once_key("codex", "overlay", "", "")
        personality.evaluate_situational_humor(.8, user_query="ordinary", session_key=711)
        mcp_server._CONTEXT_ONCE_KEYS[cache_key] = (711, 1.0)
        session = type("Session", (), {"session_id": 712})()

        async def context(**_kwargs):
            return "context"

        with patch.object(mcp_server, "ensure_repo_policy", return_value={}), patch.object(
            mcp_server, "_context_session_fingerprint", return_value=""
        ), patch.object(mcp_server.time, "time", return_value=mcp_server._CONTEXT_ONCE_TTL_SECONDS + 2.0), patch.object(
            mcp_server, "_stm_post", return_value=None
        ), patch.object(mcp_server.memory_bus, "start_session", return_value=session), patch.object(
            mcp_server, "engram_get_context", side_effect=context
        ):
            asyncio.run(mcp_server.engram_get_context_once(caller="codex", scope_key="overlay"))

        self.assertNotIn(711, personality._SESSION_STATE)


def _connection(transport_id):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={}, scope={"engram.presence.transport_id": transport_id})
        )
    )


class ContextOnceDedupesPerConversationTests(unittest.TestCase):
    """dedupe 단위는 LLM 대화(MCP 연결)다.

    2026-09-28 실측: 08:38 예약작업이 먼저 bootstrap 하자, 30분 안에 연 대화형 세션이
    그 캐시를 맞아 "already initialized" 만 받고 페르소나 없이 시작했다.
    """

    def setUp(self):
        self.sessions = iter(range(900, 950))
        self.start_session = Mock(side_effect=lambda **_kw: SimpleNamespace(session_id=next(self.sessions)))
        self.context = AsyncMock(return_value="[연속체] persona")
        self.patches = [
            patch.object(mcp_server, "ensure_repo_policy", return_value={}),
            patch.object(mcp_server, "_report_bootstrap_project", AsyncMock()),
            patch.object(mcp_server, "_context_session_fingerprint", return_value="startup:same-process"),
            patch.object(mcp_server, "_session_is_open", return_value=True),
            patch.object(mcp_server, "_mark_trusted_root_bootstrap"),
            patch.object(mcp_server, "_stm_post", return_value=None),
            patch.object(mcp_server.memory_bus, "start_session", self.start_session),
            patch.object(mcp_server, "engram_get_context", self.context),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        mcp_server._CONTEXT_ONCE_KEYS.clear()
        mcp_server._FINGERPRINT_TO_SESSION.clear()

    def _once(self, ctx):
        return asyncio.run(mcp_server.engram_get_context_once(
            caller="claude-code", scope_key="overlay", cwd=HOOK_CWD, ctx=ctx))

    def test_another_conversation_still_receives_full_context(self):
        first = self._once(_connection("scheduled-task-connection"))
        second = self._once(_connection("interactive-connection"))
        self.assertIn("[연속체]", first)
        self.assertIn("[연속체]", second)
        self.assertNotIn("already initialized", second)

    def test_same_conversation_recall_is_still_deduped(self):
        # hook 지시와 전역 CLAUDE.md 지시가 cwd 만 다르게 같은 대화에서 두 번 부른다.
        self._once(_connection("one-conversation"))
        again = asyncio.run(mcp_server.engram_get_context_once(
            caller="claude-code", scope_key="overlay", cwd=CLAUDE_MD_CWD, ctx=_connection("one-conversation")))
        self.assertIn("already initialized", again)
        self.assertEqual(self.context.await_count, 1)
        self.assertEqual(self.start_session.call_count, 1)

    def test_unidentified_caller_gets_context_without_a_new_session(self):
        first = self._once(None)
        second = self._once(None)
        self.assertIn("[연속체]", second)
        self.assertNotIn("already initialized", second)
        self.assertIn("session_id=900", second)
        self.assertEqual(self.start_session.call_count, 1)
        self.assertIn("session_id=900", first)

    def test_closed_session_never_rebinds_to_another_conversations_session(self):
        self._once(_connection("conversation-a"))   # 900
        self._once(_connection("conversation-b"))   # 901, 프로세스 fingerprint 는 이제 901
        with patch.object(mcp_server, "_session_is_open", side_effect=lambda sid, *_a: sid != 900):
            again = self._once(_connection("conversation-a"))
        self.assertIn("session_id=902", again)
        self.assertNotIn("session_id=901", again)
        self.assertEqual(self.start_session.call_args.kwargs.get("continued_from_session_id"), 900)

    def test_unidentified_caller_on_closed_session_still_gets_context(self):
        self._once(None)                            # 900
        with patch.object(mcp_server, "_session_is_open", side_effect=lambda sid, *_a: sid != 900):
            again = self._once(None)
        self.assertIn("[연속체]", again)
        self.assertIn("session_id=901", again)


if __name__ == "__main__":
    unittest.main()
