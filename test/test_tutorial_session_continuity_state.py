import unittest
from unittest.mock import patch

from core.tutorial.progress import mark_session_continuity_saved


class TutorialSessionContinuityStateTests(unittest.TestCase):
    @patch("core.tutorial.progress._save_tutorial_doc")
    @patch(
        "core.tutorial.progress._load_tutorial_doc",
        return_value={
            "version": 1,
            "state": {
                "completed_steps": ["persona_setup", "wiki_basic", "wiki_advanced"],
                "skipped_steps": [],
                "current_step": "session_continuity",
                "step_proceeded": {"session_continuity": True},
            },
        },
    )
    def test_mark_saved_records_saved_session_id(self, _mock_load_doc, _mock_save_doc):
        tutorial = mark_session_continuity_saved(source="test", session_id=321, scope_key="project:test")
        review = tutorial.get("state", {}).get("session_continuity_review", {})
        self.assertTrue(review.get("awaiting_next_session_check"))
        self.assertEqual(review.get("saved_session_id"), "321")
        self.assertEqual(review.get("saved_scope_key"), "project:test")


if __name__ == "__main__":
    unittest.main()
