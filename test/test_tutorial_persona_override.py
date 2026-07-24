import unittest
from unittest.mock import patch

from core.tutorial.progress import has_user_persona_override, refresh_tutorial_progress


class TutorialPersonaOverrideTests(unittest.TestCase):
    @patch("core.tutorial.progress._safe_load_yaml", return_value={})
    def test_empty_persona_yaml_is_not_override(self, _mock_load):
        self.assertFalse(has_user_persona_override())

    @patch("core.tutorial.progress._safe_load_yaml", return_value={"voice": "calm and warm"})
    def test_voice_value_marks_override(self, _mock_load):
        self.assertTrue(has_user_persona_override())

    @patch("core.tutorial.progress._save_tutorial_doc")
    @patch(
        "core.tutorial.progress._load_tutorial_doc",
        return_value={"version": 1, "state": {"completed_steps": [], "skipped_steps": []}},
    )
    def test_persona_step_is_not_auto_completed_even_with_override(self, _mock_load_doc, _mock_save_doc):
        tutorial = refresh_tutorial_progress(identity_name="테스터", persona_override_exists=True)
        state = tutorial.get("state", {})
        self.assertEqual(state.get("current_step"), "persona_setup")
        self.assertNotIn("persona_setup", state.get("completed_steps", []))


if __name__ == "__main__":
    unittest.main()

