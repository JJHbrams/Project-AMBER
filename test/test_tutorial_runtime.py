import unittest

from core.tutorial import build_tutorial_runtime_payload


class TutorialRuntimePayloadTests(unittest.TestCase):
    def test_persona_step_is_decision_with_two_choices(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": [],
                    "skipped_steps": [],
                },
            }
        )

        self.assertEqual(payload["current_step"], "persona_setup")
        self.assertEqual(payload["mode"], "decision")
        self.assertEqual([c["id"] for c in payload["choices"]], ["proceed", "skip"])

    def test_session_continuity_starts_with_decision_intro(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup", "wiki_basic", "wiki_advanced"],
                    "skipped_steps": [],
                },
            }
        )

        self.assertEqual(payload["current_step"], "session_continuity")
        self.assertEqual(payload["mode"], "decision")
        self.assertEqual([c["id"] for c in payload["choices"]], ["proceed", "skip"])
        self.assertIn("지금 4단계 진행", payload["choices"][0]["label"])
        self.assertIn("세션 내용 정리해서 메모리에 저장해줘", payload["prompt_to_user"])
        self.assertEqual(payload["first_input_example"], "세션 내용 정리해서 메모리에 저장해줘")
        self.assertIn("직접 입력", payload["first_input_question"])
        self.assertIn("반드시 현재 세션을 종료", payload["first_input_question"])

    def test_session_continuity_after_proceed_is_input_mode(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup", "wiki_basic", "wiki_advanced"],
                    "skipped_steps": [],
                    "step_proceeded": {"session_continuity": True},
                },
            }
        )

        self.assertEqual(payload["current_step"], "session_continuity")
        self.assertEqual(payload["mode"], "input")
        self.assertTrue(payload["input_required"])
        self.assertIn("세션 내용 정리해서 메모리에 저장해줘", payload["input_example"])
        self.assertIn("직접 입력", payload["input_question"])
        self.assertIn("반드시 닫고 새 세션", payload["input_question"])
        self.assertEqual(payload["phase"], "save_and_close")
        self.assertEqual(payload["current_session_prompt"], "세션 내용 정리해서 메모리에 저장해줘")
        self.assertEqual(payload["next_session_prompt"], "이전세션에 어떤작업을 했는지 알려줘")
        self.assertEqual(payload["next_tools"], ["engram_close_session"])

    def test_session_continuity_phase2_prompts_next_session_recall(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup", "wiki_basic", "wiki_advanced"],
                    "skipped_steps": [],
                    "step_proceeded": {"session_continuity": True},
                    "session_continuity_review": {
                        "current_session_saved": True,
                        "awaiting_next_session_check": True,
                    },
                },
            }
        )

        self.assertEqual(payload["current_step"], "session_continuity")
        self.assertEqual(payload["mode"], "input")
        self.assertEqual(payload["phase"], "next_session_recall")
        self.assertEqual(payload["input_example"], "이전세션에 어떤작업을 했는지 알려줘")
        self.assertEqual(payload["next_tools"], ["engram_verify_tutorial_session_continuity"])

    def test_wiki_basic_starts_with_decision_intro(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup"],
                    "skipped_steps": [],
                },
            }
        )

        self.assertEqual(payload["current_step"], "wiki_basic")
        self.assertEqual(payload["mode"], "decision")
        self.assertEqual([c["id"] for c in payload["choices"]], ["proceed", "skip"])
        self.assertIn("지금 2단계 진행", payload["choices"][0]["label"])

    def test_wiki_basic_after_proceed_is_input_mode(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup"],
                    "skipped_steps": [],
                    "step_proceeded": {"wiki_basic": True},
                },
            }
        )

        self.assertEqual(payload["current_step"], "wiki_basic")
        self.assertEqual(payload["mode"], "input")
        self.assertEqual(payload["choices"], [])
        self.assertTrue(payload["input_required"])
        self.assertIn("llm wiki", payload["input_example"])
        self.assertIn("절대경로", payload["input_question"])
        self.assertIn("docs", payload["wiki_docs_dir"])

    def test_wiki_advanced_has_explicit_two_choice_question(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup", "wiki_basic"],
                    "skipped_steps": [],
                },
            }
        )

        self.assertEqual(payload["current_step"], "wiki_advanced")
        self.assertEqual(payload["mode"], "decision")
        self.assertTrue(payload["await_user_choice"])
        self.assertIn("선택지:", payload["choice_question"])
        self.assertIn("1)", payload["choice_question"])
        self.assertIn("2)", payload["choice_question"])
        self.assertIn("지금 3단계 진행", payload["choices"][0]["label"])

    def test_wiki_advanced_after_proceed_is_input_mode(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": ["persona_setup", "wiki_basic"],
                    "skipped_steps": [],
                    "step_proceeded": {"wiki_advanced": True},
                },
            }
        )

        self.assertEqual(payload["current_step"], "wiki_advanced")
        self.assertEqual(payload["mode"], "input")
        self.assertTrue(payload["input_required"])
        self.assertIn("engram 프로젝트 위키", payload["input_example"])
        self.assertIn("절대경로", payload["input_question"])

    def test_completed_status_returns_completed_mode(self):
        payload = build_tutorial_runtime_payload(
            {
                "version": 1,
                "state": {
                    "completed_steps": [
                        "persona_setup",
                        "wiki_basic",
                        "wiki_advanced",
                        "session_continuity",
                    ],
                    "skipped_steps": [],
                },
            }
        )

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["mode"], "completed")
        self.assertEqual(payload["current_step"], "completed")


if __name__ == "__main__":
    unittest.main()

