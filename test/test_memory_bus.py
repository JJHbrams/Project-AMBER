import asyncio
import unittest
from unittest.mock import patch

from core.memory.bus import MemoryBus, MemorySession


class MemoryBusTests(unittest.TestCase):
    def setUp(self):
        self.bus = MemoryBus()

    @patch("core.memory.bus.create_session", return_value=42)
    def test_start_session_keeps_scope(self, mock_create_session):
        session = self.bus.start_session("discord:123")

        self.assertEqual(session, MemorySession(session_id=42, scope_key="discord:123"))
        mock_create_session.assert_called_once_with(scope_key="discord:123", project_keys=None)

    @patch("core.memory.bus.resolve_scope_key", return_value="project:auto-1234")
    @patch("core.memory.bus.create_session", return_value=52)
    def test_start_session_resolves_project_scope(self, mock_create_session, mock_resolve_scope_key):
        session = self.bus.start_session()

        self.assertEqual(session, MemorySession(session_id=52, scope_key="project:auto-1234"))
        mock_resolve_scope_key.assert_called_once_with(None, project_key=None, cwd=None)
        mock_create_session.assert_called_once_with(scope_key="project:auto-1234", project_keys=None)

    @patch("core.memory.bus.resolve_project_key", return_value="project-key")
    @patch("core.memory.bus.build_system_prompt", return_value="prompt")
    def test_compose_prompt_context_uses_session_scope(self, mock_build_system_prompt, mock_resolve_project_key):
        session = MemorySession(session_id=7, scope_key="default:main")

        # compose_prompt_context는 async(내부 SemanticGraph 호출이 await 기반) — asyncio.run으로 실행.
        result = asyncio.run(self.bus.compose_prompt_context("hello", caller="copilot-cli", session=session))

        self.assertEqual(result, "prompt")
        mock_resolve_project_key.assert_called_once_with(cwd=None)
        mock_build_system_prompt.assert_called_once_with(
            "hello",
            caller="copilot-cli",
            scope_key="default:main",
            project_key="project-key",
            is_session_init=False,
        )

    @patch("core.memory.bus.resolve_project_key", return_value="project-key")
    @patch("core.memory.bus.resolve_scope_key", return_value="project:auto-5678")
    @patch("core.memory.bus.build_system_prompt", return_value="prompt")
    def test_compose_prompt_context_resolves_scope_without_session(
        self,
        mock_build_system_prompt,
        mock_resolve_scope_key,
        mock_resolve_project_key,
    ):
        result = asyncio.run(self.bus.compose_prompt_context("hello", caller="copilot-cli"))

        self.assertEqual(result, "prompt")
        mock_resolve_scope_key.assert_called_once_with(None, project_key=None, cwd=None)
        mock_resolve_project_key.assert_called_once_with(cwd=None)
        mock_build_system_prompt.assert_called_once_with(
            "hello",
            caller="copilot-cli",
            scope_key="project:auto-5678",
            project_key="project-key",
            is_session_init=False,
        )

    @patch("core.memory.bus.append_working_memory_hint")
    @patch("core.memory.bus.save_message")
    def test_record_assistant_message_updates_working_memory(
        self,
        mock_save_message,
        mock_append_working_memory_hint,
    ):
        session = MemorySession(session_id=9, scope_key="default:main")

        self.bus.record_assistant_message(
            session,
            "reply",
            user_content="question",
            update_working_memory=True,
        )

        mock_save_message.assert_called_once_with(9, "assistant", "reply")
        mock_append_working_memory_hint.assert_called_once_with("default:main", "question", "reply")

    @patch("core.memory.bus.save_memory")
    def test_maybe_save_episodic_memory_obeys_cadence(self, mock_save_memory):
        saved = self.bus.maybe_save_episodic_memory(11, "user", "assistant", user_turn_count=4, cadence=4)
        skipped = self.bus.maybe_save_episodic_memory(11, "user", "assistant", user_turn_count=3, cadence=4)

        self.assertTrue(saved)
        self.assertFalse(skipped)
        mock_save_memory.assert_called_once_with(11, "Q: user / A: assistant")


if __name__ == "__main__":
    unittest.main()

