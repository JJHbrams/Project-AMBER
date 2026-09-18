"""context-once 캐시 키 회귀.

배경: 전역 ~/.claude/CLAUDE.md 는 고정 cwd 를, SessionStart hook 은 프로젝트별
cwd 를 지시한다. 둘 다 같은 scope_key='overlay' 로 bootstrap 하는데 cwd 가
캐시 키에 들어 있어서 매 세션 bootstrap 이 두 번 실행되고 STM 세션이 하나 더
생겼다(2026-09-17 실측: 하루 5개 중 2개가 이 원인).

scope_key 가 명시되면 resolve_scope_key 는 cwd 를 보지 않으므로, 두 호출은
같은 컨텍스트를 받는다. 그러면 캐시도 갈리면 안 된다.
"""

import unittest

from mcp_server import _build_context_once_key


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


if __name__ == "__main__":
    unittest.main()
