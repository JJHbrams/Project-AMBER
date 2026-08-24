import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with patch("core.storage.db.initialize_db"), patch.object(
    urllib.request,
    "urlopen",
    side_effect=OSError("isolated test"),
):
    import mcp_server as server


class MCPDirectiveToolTests(unittest.TestCase):
    @patch.object(server, "add_directive")
    @patch.object(server, "begin_registration", return_value={"status": "draft_started", "draft_id": "draft-1"})
    def test_add_tool_starts_draft_and_never_calls_persistent_mutator(self, begin, persistent_add):
        result = server.engram_add_directive("demo", "rule", "test-agent", "session-a")
        self.assertEqual(result["status"], "registration_required")
        self.assertEqual(result["draft_id"], "draft-1")
        begin.assert_called_once()
        persistent_add.assert_not_called()

    @patch.object(server, "begin_registration", return_value={"status": "draft_started", "draft_id": "draft-2"})
    @patch.object(server, "get_directive", return_value={"key": "demo", "content": "old"})
    def test_update_tool_starts_draft_without_persisting(self, _existing, begin):
        result = server.engram_update_directive("demo", "test-agent", "session-a", content="new")
        self.assertEqual(result["status"], "registration_required")
        self.assertEqual(begin.call_args.args[2]["content"], "new")

    @patch.object(server, "preflight_directives", return_value={"decision": "allow"})
    def test_preflight_tool_passes_structured_guard_context(self, mock_preflight):
        server.engram_preflight_directives(
            action="apply patch",
            cwd="C:/repo",
            action_metadata_json='{"mode":"repo-write","tags":["new-independent-task"]}',
            chore_intent_json='{"is_chore":true,"summary":"dependency bump"}',
            independent_task_context_json='{"requested":true,"existing_changes_owner":"other-task"}',
            execute_guards=True,
        )

        _, kwargs = mock_preflight.call_args
        self.assertEqual(kwargs["cwd"], "C:/repo")
        self.assertEqual(kwargs["action_metadata"]["mode"], "repo-write")
        self.assertEqual(kwargs["action_metadata"]["tags"], ["new-independent-task"])
        self.assertTrue(kwargs["chore_intent"]["is_chore"])
        self.assertTrue(kwargs["independent_task_context"]["requested"])
        self.assertEqual(
            kwargs["independent_task_context"]["existing_changes_owner"],
            "other-task",
        )
        self.assertTrue(kwargs["execute_guards"])


class MCPContextBootstrapTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(server, "get_persona_status", return_value={"initialized": True})
    @patch.object(server, "get_identity", return_value={"name": ""})
    @patch.object(server.memory_bus, "compose_prompt_context", new_callable=AsyncMock, return_value="context")
    @patch.object(server, "ensure_repo_policy", return_value={"ok": True, "changed": True})
    async def test_plain_context_ensures_repo_policy_and_propagates_cwd(
        self,
        ensure_policy,
        compose_prompt_context,
        _get_identity,
        _get_persona_status,
    ):
        result = await server.engram_get_context(
            user_query="inspect context",
            caller="claude-code",
            scope_key="scope-a",
            project_key="project-a",
            cwd="C:/repo",
        )

        ensure_policy.assert_called_once_with("C:/repo")
        compose_prompt_context.assert_awaited_once_with(
            "inspect context",
            caller="claude-code",
            scope_key="scope-a",
            project_key="project-a",
            cwd="C:/repo",
            is_session_init=True,
        )
        self.assertIn("context", result)

    @patch.object(server, "get_persona_status", return_value={"initialized": True})
    @patch.object(server, "get_identity", return_value={"name": ""})
    @patch.object(server.memory_bus, "compose_prompt_context", new_callable=AsyncMock, return_value="context")
    @patch.object(server, "ensure_repo_policy", return_value={"ok": False, "reason": "hook unavailable"})
    async def test_plain_context_logs_repo_policy_bootstrap_failure(
        self,
        _ensure_policy,
        _compose_prompt_context,
        _get_identity,
        _get_persona_status,
    ):
        with self.assertLogs("mcp_server", level="WARNING") as captured:
            await server.engram_get_context(user_query="inspect context", cwd="C:/repo")

        self.assertIn("repo policy bootstrap 실패", "\n".join(captured.output))

    @patch.object(server, "engram_get_context", new_callable=AsyncMock, return_value="context")
    @patch.object(server, "_stm_post", return_value={"session_id": 42})
    @patch.object(server, "_context_session_fingerprint", return_value="repo-policy-test")
    @patch.object(server, "ensure_repo_policy", return_value={"ok": True, "changed": True})
    async def test_context_once_ensures_repo_policy_before_loading_context(
        self,
        ensure_policy,
        _fingerprint,
        _stm_post,
        _get_context,
    ):
        with server._CONTEXT_ONCE_LOCK:
            server._CONTEXT_ONCE_KEYS.clear()

        result = await server.engram_get_context_once(cwd="C:/repo")

        ensure_policy.assert_called_once_with("C:/repo")
        self.assertIn("session_id=42", result)


if __name__ == "__main__":
    unittest.main()
