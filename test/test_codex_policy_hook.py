import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.integrations.agent_policy_hook import process_provider_hook_input, provider_hook_main
from core.integrations.policy_preflight import classify_agent_pretool_payload, process_policy_request


ROOT = Path(__file__).resolve().parents[1]


class CodexPolicyHookTests(unittest.TestCase):
    def test_codex_bash_reuses_git_parser_and_provider_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            result = classify_agent_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "git commit"}},
                str(repo),
                "codex",
            )

        self.assertTrue(result["classified"])
        request = result["request"]
        self.assertEqual(request["caller"], "codex")
        self.assertEqual(request["action"], "codex-pretool")
        self.assertEqual(request["action_metadata"]["category"], "codex-pretool")
        self.assertIn("codex-pretool", request["action_metadata"]["tags"])
        self.assertTrue(request["advisory_only"])

    def test_apply_patch_extracts_supported_headers_and_excludes_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            patch_text = "\n".join(
                (
                    "*** Begin Patch",
                    "*** Add File: src/new.py",
                    "+print('new')",
                    "*** Update File: ../outside.py",
                    "*** Delete File: old.py",
                    "*** End Patch",
                )
            )
            result = classify_agent_pretool_payload(
                {"tool_name": "apply_patch", "tool_input": {"patch": patch_text}},
                str(repo),
                "codex",
            )

        self.assertTrue(result["classified"])
        self.assertEqual(result["hook"]["target_paths"], ["src/new.py", "old.py"])
        self.assertEqual(result["hook"]["excluded_target_paths"], ["../outside.py"])
        self.assertEqual(result["request"]["caller"], "codex")

    def test_apply_patch_with_only_outside_targets_is_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            result = classify_agent_pretool_payload(
                {
                    "tool_name": "apply_patch",
                    "tool_input": {"patch": "*** Delete File: ../outside.py"},
                },
                str(repo),
                "codex",
            )

        self.assertFalse(result["classified"])
        self.assertIn("outside", result["reason"])

    def test_codex_hook_request_is_always_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            with patch("core.integrations.policy_preflight.get_cfg_value", return_value=True), patch(
                "core.integrations.policy_preflight.preflight_directives",
                return_value={"decision": "allow"},
            ) as preflight:
                output, _, _ = process_policy_request(
                    {
                        "request_type": "codex-pretool-hook",
                        "cwd": str(repo),
                        "hook_payload": {
                            "tool_name": "apply_patch",
                            "tool_input": {"patch": "*** Update File: README.md"},
                        },
                    }
                )

        self.assertTrue(output["classified"])
        self.assertTrue(preflight.call_args.kwargs["advisory_only"])

    def test_codex_risk_uses_additional_context_without_block_contract(self):
        policy_result = {
            "decision": "advisory",
            "policy_decision": "blocked",
            "would_block": True,
            "reason": "protected branch",
            "classified": True,
            "guidance_level": "warn",
        }
        with patch(
            "core.integrations.agent_policy_hook.process_policy_request",
            return_value=(policy_result, 0, ""),
        ):
            stdout, stderr, exit_code = process_provider_hook_input("{}", "codex")

        payload = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"],
            "PreToolUse",
        )
        self.assertIn("protected branch", payload["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", stdout)
        self.assertNotIn('"decision"', stdout)

    def test_enforce_agents_denies_codex_and_claude_tool_calls_with_exit_zero(self):
        policy_result = {
            "decision": "advisory",
            "policy_decision": "workflow_required",
            "would_block": True,
            "reason": "create a feature branch first",
            "classified": True,
            "guidance_level": "enforce_agents",
        }
        with patch(
            "core.integrations.agent_policy_hook.process_policy_request",
            return_value=(policy_result, 0, ""),
        ):
            codex_stdout, codex_stderr, codex_exit = process_provider_hook_input("{}", "codex")
            claude_stdout, claude_stderr, claude_exit = process_provider_hook_input("{}", "claude-code")

        for stdout, stderr, exit_code in (
            (codex_stdout, codex_stderr, codex_exit),
            (claude_stdout, claude_stderr, claude_exit),
        ):
            payload = json.loads(stdout)["hookSpecificOutput"]
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["permissionDecision"], "deny")
            self.assertIn("feature branch", payload["permissionDecisionReason"])

    def test_enforcement_mode_fails_open_on_policy_or_backend_error(self):
        for policy_result, backend_exit_code in (
            ({
                "decision": "error",
                "policy_decision": "error",
                "would_block": True,
                "reason": "policy database unavailable",
                "classified": True,
                "guidance_level": "enforce_agents",
            }, 0),
            ({
                "decision": "advisory",
                "policy_decision": "blocked",
                "would_block": True,
                "reason": "backend failed",
                "classified": True,
                "guidance_level": "enforce_agents",
            }, 1),
        ):
            with self.subTest(policy_result=policy_result), patch(
                "core.integrations.agent_policy_hook.process_policy_request",
                return_value=(policy_result, backend_exit_code, ""),
            ):
                stdout, _stderr, exit_code = process_provider_hook_input("{}", "codex")
            hook_output = json.loads(stdout)["hookSpecificOutput"]
            self.assertEqual(exit_code, 0)
            self.assertNotIn("permissionDecision", hook_output)
            self.assertIn("additionalContext", hook_output)

    def test_all_adapter_errors_remain_nonblocking(self):
        with patch(
            "core.integrations.agent_policy_hook.process_policy_request",
            side_effect=RuntimeError("backend unavailable"),
        ):
            codex_stdout, codex_stderr, codex_exit = process_provider_hook_input("{}", "codex")
            claude_stdout, claude_stderr, claude_exit = process_provider_hook_input("{}", "claude-code")

        self.assertEqual(codex_exit, 0)
        self.assertEqual(claude_exit, 0)
        self.assertEqual(codex_stderr, "")
        self.assertEqual(claude_stdout, "")
        self.assertIn("backend unavailable", json.loads(codex_stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertIn("backend unavailable", claude_stderr)

    def test_malformed_codex_payload_warns_but_exits_zero(self):
        stdout, stderr, exit_code = process_provider_hook_input("not-json", "codex")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("malformed Codex hook payload JSON", json.loads(stdout)["hookSpecificOutput"]["additionalContext"])

    def test_allow_and_unclassified_codex_results_emit_no_output(self):
        for result in (
            {"decision": "allow", "classified": True},
            {"decision": "allow", "classified": False, "reason": "read-only"},
        ):
            with self.subTest(result=result), patch(
                "core.integrations.agent_policy_hook.process_policy_request",
                return_value=(result, 0, ""),
            ):
                self.assertEqual(process_provider_hook_input("{}", "codex"), ("", "", 0))

    def test_provider_hook_main_never_returns_backend_exit_code(self):
        stdin = io.StringIO("not-json")
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = provider_hook_main("codex", stdin=stdin, stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_multicall_entrypoint_codex_role_is_nonblocking(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "engram_overlay_entry.py"),
                "--role",
                "agent-policy-hook",
                "--provider",
                "codex",
            ],
            input="not-json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["hookEventName"], "PreToolUse")


if __name__ == "__main__":
    unittest.main()
