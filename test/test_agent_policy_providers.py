"""Every CLI provider Engram ships a shim for must receive policy guidance.

`warn` guidance is only useful if it reaches the model. Claude-shaped providers
receive `hookSpecificOutput.additionalContext`; Antigravity requires its own
top-level allow/deny response.
"""

from __future__ import annotations

import json
import os
import subprocess
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
            payload = json.loads(stdout)
            if spec["dialect"] == "antigravity":
                self.assertEqual(payload["decision"], "allow")
                self.assertIn("protected branch", payload["reason"])
            else:
                hook_output = payload["hookSpecificOutput"]
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
            if spec["dialect"] == "antigravity":
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

    def test_legacy_gemini_provider_alias_emits_bounded_deprecation_log(self):
        with patch("core.integrations.agent_policy_hook.logger.warning") as warning, patch(
            "core.integrations.agent_policy_hook.process_policy_request",
            return_value=({"decision": "allow", "guidance_level": "warn"}, 0, ""),
        ):
            stdout, _, _ = process_provider_hook_input("{}", "gemini")
        self.assertEqual(json.loads(stdout)["decision"], "allow")
        warning.assert_called_once()


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
            ("antigravity", "run_command"),
        ):
            with self.subTest(provider=provider, tool=tool_name):
                result = self._classify(provider, tool_name, "git commit -m x")
                self.assertTrue(
                    result.get("classified") or "worktree" in str(result.get("reason", "")),
                    f"{tool_name} was dropped as out of scope: {result.get('reason')}",
                )
                self.assertEqual(result["hook"]["tool_name"], tool_name)

    def test_readonly_tool_is_still_out_of_scope(self):
        result = self._classify("antigravity", "read_file", "")
        self.assertFalse(result["classified"])


class AntigravityNativePayloadTests(unittest.TestCase):
    """Exercise AGY's native camel-case ``toolCall`` payload end-to-end.

    The policy engine is only stubbed after classification, so every case still
    traverses the provider adapter, native extractor, path/command classifier,
    and Antigravity stdout contract.
    """

    @staticmethod
    def _payload(name: str, args: dict) -> str:
        return json.dumps({"toolCall": {"name": name, "args": args}})

    @staticmethod
    def _init_repo(path: Path, branch: str) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True, text=True)

    @staticmethod
    def _blocked_policy() -> dict:
        return {
            "decision": "advisory", "policy_decision": "blocked", "would_block": True,
            "advisory_only": True, "blocking_guards": [{"content": "protected branch"}],
        }

    def test_native_agy_write_tools_and_shell_enforce_by_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp) / "protected"
            feature = Path(tmp) / "feature"
            for repo, branch in ((protected, "main"), (feature, "feat/native-agy")):
                self._init_repo(repo, branch)
                (repo / "note.txt").write_text("before", encoding="utf-8")

            def policy_result(**kwargs):
                if Path(kwargs["cwd"]).resolve().is_relative_to(protected.resolve()):
                    return self._blocked_policy()
                return {"decision": "allow", "reason": ""}

            protected_cases = (
                ("run_command", {"Cwd": str(protected), "CommandLine": "git commit -m blocked"}),
                ("write_to_file", {"Cwd": str(protected), "TargetFile": str(protected / "note.txt")}),
                ("replace_file_content", {"Cwd": str(protected), "TargetFile": str(protected / "note.txt")}),
                ("multi_replace_file_content", {"Cwd": str(protected), "TargetFile": str(protected / "note.txt")}),
            )
            with patch("core.integrations.policy_preflight.get_cfg_value", return_value="enforce_agents"), patch(
                "core.integrations.policy_preflight.preflight_directives", side_effect=policy_result
            ):
                for name, args in protected_cases:
                    with self.subTest(tool=name):
                        stdout, stderr, code = process_provider_hook_input(self._payload(name, args), "antigravity")
                        self.assertEqual((stderr, code), ("", 0))
                        response = json.loads(stdout)
                        self.assertEqual(response["decision"], "deny")
                        self.assertIn("protected branch", response["reason"])

                stdout, _, _ = process_provider_hook_input(
                    self._payload("write_to_file", {"Cwd": str(feature), "TargetFile": str(feature / "note.txt")}),
                    "antigravity",
                )
                self.assertEqual(json.loads(stdout), {"decision": "allow"})

                stdout, _, _ = process_provider_hook_input(
                    self._payload("view_file", {"Cwd": str(protected), "TargetFile": str(protected / "note.txt")}),
                    "antigravity",
                )
                self.assertEqual(json.loads(stdout), {"decision": "allow"})

    def test_native_agy_warn_keeps_reason_but_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "protected"
            self._init_repo(repo, "main")
            with patch("core.integrations.policy_preflight.get_cfg_value", return_value="warn"), patch(
                "core.integrations.policy_preflight.preflight_directives",
                return_value=self._blocked_policy(),
            ):
                stdout, stderr, code = process_provider_hook_input(
                    self._payload("run_command", {"Cwd": str(repo), "CommandLine": "git commit -m x"}),
                    "antigravity",
                )
        self.assertEqual((stderr, code), ("", 0))
        response = json.loads(stdout)
        self.assertEqual(response["decision"], "allow")
        self.assertIn("protected branch", response["reason"])


class ProviderHookSyncTests(unittest.TestCase):
    def test_legacy_gemini_sync_alias_migrates_to_antigravity(self):
        user_entry = {"hooks": [{"type": "command", "command": "user-tool"}]}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings_path = home / ".gemini" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps({"hooks": {"BeforeTool": [user_entry]}}), encoding="utf-8")
            hooks_path = home / ".gemini" / "config" / "hooks.json"
            with patch.object(engram_bootstrap, "_GEMINI_SETTINGS_PATH", settings_path), patch.object(
                engram_bootstrap, "_ANTIGRAVITY_HOOKS_PATH", hooks_path
            ), patch.object(engram_bootstrap, "_ENGRAM_DIR", home / ".engram"
            ), patch.dict(
                engram_bootstrap._PROVIDER_HOOK_SCRIPT_PATHS,
                {
                    "gemini": (
                        home / ".engram" / "engram-gemini-pretool-hook.ps1",
                        home / ".engram" / "engram-gemini-pretool-hook.sh",
                    ),
                    "antigravity": (
                        home / ".engram" / "engram-antigravity-pretool-hook.ps1",
                        home / ".engram" / "engram-antigravity-pretool-hook.sh",
                    ),
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
        self.assertEqual(len(entries), 1, "only the user's legacy BeforeTool hook must survive")
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
