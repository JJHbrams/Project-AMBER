import unittest

from core.integrations.engram_bootstrap import bubble_bootstrap_prompt
from overlay.bubble.session import BubbleSessionManager


class BubbleBootstrapTests(unittest.TestCase):
    def test_bubble_bootstrap_is_always_present(self):
        prompt = bubble_bootstrap_prompt("C:/workspace/project")

        self.assertIsNotNone(prompt)
        self.assertIn("engram_get_context_once", prompt)
        self.assertIn("caller='claude-code'", prompt)
        self.assertIn("cwd='C:/workspace/project'", prompt)
        self.assertNotIn("engram_report_session_title", prompt)
        self.assertNotIn("first substantive user request", prompt)
        self.assertIn("Do not generate or report a session title", prompt)
        self.assertNotIn("do not repeat engram_get_context_once", prompt.lower())

    def test_codex_bubble_uses_codex_caller_and_same_title_policy(self):
        prompt = bubble_bootstrap_prompt("C:/workspace/project", caller="Codex")

        self.assertIn("caller='Codex'", prompt)
        self.assertIn("engram_get_context_once", prompt)
        self.assertIn("Do not generate or report a session title", prompt)
        self.assertNotIn("engram_report_session_title", prompt)

    def test_claude_fresh_and_resume_rebuild_the_same_bootstrap(self):
        prompt = bubble_bootstrap_prompt("C:/workspace/project")
        fresh = BubbleSessionManager("C:/workspace/project", bootstrap_prompt=prompt)
        resumed = BubbleSessionManager(
            "C:/workspace/project", bootstrap_prompt=prompt, resume_session_id="prior-session"
        )

        self.assertEqual(fresh._build_options().append_system_prompt, resumed._build_options().append_system_prompt)
        self.assertIn("engram_get_context_once", resumed._build_options().append_system_prompt)
        self.assertNotIn("do not repeat engram_get_context_once", resumed._build_options().append_system_prompt.lower())


if __name__ == "__main__":
    unittest.main()
