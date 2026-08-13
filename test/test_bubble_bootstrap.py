import unittest

from core.integrations.engram_bootstrap import bubble_bootstrap_prompt


class BubbleBootstrapTests(unittest.TestCase):
    def test_bubble_bootstrap_is_always_present(self):
        prompt = bubble_bootstrap_prompt("C:/workspace/project")

        self.assertIsNotNone(prompt)
        self.assertIn("engram_get_context_once", prompt)
        self.assertIn("caller='claude-code'", prompt)
        self.assertIn("cwd='C:/workspace/project'", prompt)


if __name__ == "__main__":
    unittest.main()
