"""Every CLI provider Engram ships a shim for must receive policy guidance.

`warn` guidance is only useful if it reaches the model. Each supported runtime
injects `hookSpecificOutput.additionalContext`, so the adapter emits that for all
providers; only the denial contract differs (Gemini has no `permissionDecision`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrations import engram_bootstrap
from core.integrations.agent_policy_hook import _PROVIDERS, process_provider_hook_input
from core.integrations.policy_preflight import classify_agent_pretool_payload


_WARN_RESULT = {
    "decision": "advisory",
    "policy_decision": "blocked",
    "would_block": True,
    "reason": "protected branch",
    "classified": True,
    "guidance_level": "warn",
}
_ENFORCE_RESULT = {
    **_WARN_RESULT,
    "policy_decision": "workflow_required",
    "guidance_level": "enforce_agents",
}


class ProviderGuidanceContractTests(unittest.TestCase):
    def test_warn_reaches_the_model_on_stdout_for_every_provider(self):
        for provider, spec in _PROVIDERS.items():
            with self.subTest(provider=provider), patch(
                "core.integrations.agent_policy_hook.process_policy_request",
                return_value=(_WARN_RESULT, 0, ""),
            ):
                stdout, stderr, exit_code = process_provider_hook_input("{}", provider)
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            hook_output = json.loads(stdout)["hookSpecificOutput"]
            self.assertEqual(hook_output["hookEventName"], spec["event"])
            self.assertIn("protected branch", hook_output["additionalContext"])
            self.assertNotIn("permissionDecision", hook_output)

    def test_enforcement_uses_each_runtime_denial_contract(self):
        for provider, spec in _PROVIDERS.items():
            with self.subTest(provider=provider), patch(
                "core.integrations.agent_policy_hook.process_policy_request",
                return_value=(_ENFORCE_RESULT, 0, ""),
            ):
                stdout, stderr, exit_code = process_provider_hook_input("{}", provider)
            self.assertEqual(exit_code, 0, "denial must never ride the exit code")
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            if spec["dialect"] == "gemini":
                self.assertEqual(payload["decision"], "deny")
                self.assertIn("protected branch", payload["reason"])
            else:
                hook_output = payload["hookSpecificOutput"]
                self.assertEqual(hook_output["hookEventName"], spec["event"])
                self.assertEqual(hook_output["permissionDecision"], "deny")
                self.assertIn("protected branch", hook_output["permissionDecisionReason"])

    def test_unsupported_provider_stays_silent_and_nonblocking(self):
        stdout, stderr, exit_code = process_provider_hook_input("{}", "goose")
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("unsupported agent policy provider", stderr)


class ProviderToolAliasTests(unittest.TestCase):
    """Providers name the same repo-write tools differently; the classifier normalizes them."""

    def _classify(self, provider: str, tool_name: str, command: str) -> dict:
        return classify_agent_pretool_payload(
            {"tool_name": tool_name, "tool_input": {"command": command}},
            str(ROOT),
            provider,
        )

    def test_shell_tool_aliases_reach_the_git_classifier(self):
        for provider, tool_name in (
            ("claude-code", "Bash"),
            ("codex", "Bash"),
            ("copilot", "bash"),
            ("gemini", "run_shell_command"),
        ):
            with self.subTest(provider=provider, tool=tool_name):
                result = self._classify(provider, tool_name, "git commit -m x")
                self.assertTrue(
                    result.get("classified") or "worktree" in str(result.get("reason", "")),
                    f"{tool_name} was dropped as out of scope: {result.get('reason')}",
                )
                self.assertEqual(result["hook"]["tool_name"], tool_name)

    def test_readonly_tool_is_still_out_of_scope(self):
        result = self._classify("gemini", "read_file", "")
        self.assertFalse(result["classified"])


class ProviderHookSyncTests(unittest.TestCase):
    def test_gemini_sync_is_idempotent_and_preserves_user_hooks(self):
        user_entry = {"hooks": [{"type": "command", "command": "user-tool"}]}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".gemini" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps({"hooks": {"BeforeTool": [user_entry]}}), encoding="utf-8"
            )
            with patch.object(engram_bootstrap, "_GEMINI_SETTINGS_PATH", settings_path), patch.object(
                engram_bootstrap, "_ENGRAM_DIR", home / ".engram"
            ), patch.dict(
                engram_bootstrap._PROVIDER_HOOK_SCRIPT_PATHS,
                {
                    "gemini": (
                        home / ".engram" / "engram-gemini-pretool-hook.ps1",
                        home / ".engram" / "engram-gemini-pretool-hook.sh",
                    )
                },
            ):
                first = engram_bootstrap.sync_gemini_pretool_hook(True)
                second = engram_bootstrap.sync_gemini_pretool_hook(True)
                entries = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["BeforeTool"]
                removed = engram_bootstrap.sync_gemini_pretool_hook(False)
                after = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["BeforeTool"]

        self.assertTrue(first["ok"] and first["changed"])
        self.assertTrue(second["ok"])
        self.assertFalse(second["changed"], "second sync must be a no-op")
        self.assertEqual(len(entries), 2, "the user's own BeforeTool hook must survive")
        self.assertEqual(after, [user_entry])
        self.assertTrue(removed["ok"])

    def test_copilot_sync_owns_its_own_hook_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hooks_path = home / ".copilot" / "hooks" / "engram.json"
            with patch.object(engram_bootstrap, "_COPILOT_HOOKS_PATH", hooks_path), patch.object(
                engram_bootstrap, "_ENGRAM_DIR", home / ".engram"
            ), patch.dict(
                engram_bootstrap._PROVIDER_HOOK_SCRIPT_PATHS,
                {
                    "copilot": (
                        home / ".engram" / "engram-copilot-pretool-hook.ps1",
                        home / ".engram" / "engram-copilot-pretool-hook.sh",
                    )
                },
            ):
                first = engram_bootstrap.sync_copilot_pretool_hook(True)
                payload = json.loads(hooks_path.read_text(encoding="utf-8"))
                second = engram_bootstrap.sync_copilot_pretool_hook(True)
                engram_bootstrap.sync_copilot_pretool_hook(False)
                still_there = hooks_path.exists()

        self.assertTrue(first["ok"] and first["changed"])
        self.assertFalse(second["changed"], "second sync must be a no-op")
        self.assertEqual(payload["version"], 1)
        handler = payload["hooks"]["PreToolUse"][0]
        self.assertEqual(handler["type"], "command")
        command_key = "powershell" if os.name == "nt" else "command"
        self.assertIn("engram-copilot-pretool-hook", handler[command_key])
        self.assertFalse(still_there, "disabling guidance must remove the managed file")


if __name__ == "__main__":
    unittest.main()
