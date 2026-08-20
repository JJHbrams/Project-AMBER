import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.integrations.engram_bootstrap import _render_pretool_hook_script
from core.integrations.policy_preflight import (
    HookPayloadError,
    classify_agent_pretool_payload,
    classify_claude_pretool_payload,
    process_policy_request,
)


ROOT = Path(__file__).resolve().parents[1]


class PolicyPreflightTests(unittest.TestCase):
    def _hook_payload(self, tool_name: str, command: str) -> dict:
        return {
            "tool_name": tool_name,
            "tool_input": {
                "command": command,
            },
        }

    def _run_policy_preflight(self, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "engram_overlay_entry.py"), "--role", "policy-preflight", *args],
            cwd=ROOT,
            shell=False,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def _run_rendered_wrapper(self, hook_payload_text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "engram-claude-pretool-hook.ps1"
            script_path.write_text(_render_pretool_hook_script(), encoding="utf-8")
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                cwd=ROOT,
                shell=False,
                input=hook_payload_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )

    def test_guidance_toggle_disables_claude_and_git_hook_evaluation(self):
        with patch("core.integrations.policy_preflight.get_cfg_value", return_value=False):
            claude_output, claude_exit, claude_stderr = process_policy_request(
                {"request_type": "claude-pretool-hook", "hook_payload": "{bad-json"}
            )
            git_output, git_exit, git_stderr = process_policy_request(
                {"caller": "git-hook", "cwd": "missing", "execute_guards": True}
            )

        self.assertEqual(claude_exit, 0)
        self.assertEqual(git_exit, 0)
        self.assertEqual(claude_stderr, "")
        self.assertEqual(git_stderr, "")
        self.assertFalse(claude_output["guidance_enabled"])
        self.assertFalse(git_output["guidance_enabled"])
        self.assertEqual(claude_output["guidance_level"], "off")

    def test_guidance_toggle_also_disables_codex_hook_evaluation(self):
        with patch("core.integrations.policy_preflight.get_cfg_value", return_value=False):
            output, exit_code, stderr_reason = process_policy_request(
                {"request_type": "codex-pretool-hook", "hook_payload": "{bad-json"}
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr_reason, "")
        self.assertFalse(output["guidance_enabled"])
        self.assertEqual(output["guidance_level"], "off")
        self.assertEqual(output["request_type"], "codex-pretool-hook")

    def test_agent_enforcement_level_still_runs_policy_as_advisory_for_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            with patch(
                "core.integrations.policy_preflight.get_cfg_value",
                return_value="enforce_agents",
            ), patch(
                "core.integrations.policy_preflight.preflight_directives",
                return_value={"decision": "blocked", "reason": "protected branch"},
            ) as preflight:
                output, _, _ = process_policy_request(
                    {
                        "request_type": "codex-pretool-hook",
                        "cwd": str(repo),
                        "hook_payload": self._hook_payload("apply_patch", "*** Update File: README.md"),
                    }
                )

        self.assertEqual(output["guidance_level"], "enforce_agents")
        self.assertTrue(preflight.call_args.kwargs["advisory_only"])

    def test_claude_classifier_remains_a_compatibility_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            payload = self._hook_payload("Bash", "git commit")

            legacy = classify_claude_pretool_payload(payload, str(repo))
            generalized = classify_agent_pretool_payload(payload, str(repo), "claude-code")

        self.assertEqual(legacy, generalized)
        self.assertEqual(legacy["request"]["caller"], "claude-code")
        self.assertEqual(legacy["request"]["action"], "claude-pretool")

    def test_nested_shell_git_write_commands_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            for tool_name, command in (
                ("Bash", "cmd /c git commit"),
                ("PowerShell", "powershell -Command git commit"),
            ):
                with self.subTest(command=command):
                    result = classify_claude_pretool_payload(self._hook_payload(tool_name, command), str(repo))
                    self.assertTrue(result["classified"])
                    self.assertEqual(result["hook"]["git_write_subcommand"], "commit")
                    self.assertEqual(result["hook"]["git_worktree_root"], str(repo.resolve()))

    def test_agent_merge_policy_requires_explicit_no_ff_and_rejects_ff_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            with patch(
                "core.integrations.policy_preflight.get_cfg_value",
                return_value="enforce_agents",
            ):
                missing, missing_exit, _ = process_policy_request(
                    {
                        "request_type": "codex-pretool-hook",
                        "cwd": str(repo),
                        "hook_payload": self._hook_payload("PowerShell", "git merge feat/example"),
                    }
                )
                ff_only, ff_only_exit, _ = process_policy_request(
                    {
                        "request_type": "claude-pretool-hook",
                        "cwd": str(repo),
                        "hook_payload": self._hook_payload(
                            "Bash",
                            "git merge --no-ff --ff-only feat/example",
                        ),
                    }
                )

            self.assertEqual(missing_exit, 0)
            self.assertEqual(ff_only_exit, 0)
            self.assertEqual(missing["policy_decision"], "blocked")
            self.assertTrue(missing["would_block"])
            self.assertFalse(missing["hook"]["merge_has_no_ff"])
            self.assertEqual(ff_only["policy_decision"], "blocked")
            self.assertTrue(ff_only["hook"]["merge_has_ff_only"])

    def test_agent_merge_with_no_ff_continues_to_regular_policy_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            with patch(
                "core.integrations.policy_preflight.get_cfg_value",
                return_value="enforce_agents",
            ), patch(
                "core.integrations.policy_preflight.preflight_directives",
                return_value={"decision": "allow"},
            ) as preflight:
                output, exit_code, _ = process_policy_request(
                    {
                        "request_type": "codex-pretool-hook",
                        "cwd": str(repo),
                        "hook_payload": self._hook_payload(
                            "PowerShell",
                            "git merge --no-ff feat/example",
                        ),
                    }
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(output["decision"], "allow")
            self.assertTrue(output["hook"]["merge_has_no_ff"])
            preflight.assert_called_once()

    def test_safe_read_only_git_commands_remain_out_of_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            for tool_name, command in (
                ("Bash", "git status"),
                ("Bash", "git log --oneline -5"),
                ("Bash", "git diff --stat"),
                ("Bash", "git show HEAD~1 --stat"),
                ("Bash", "git branch"),
                ("Bash", "git branch --show-current"),
                ("Bash", "git rev-parse --show-toplevel"),
                ("Bash", "cmd /c git status"),
                ("PowerShell", "powershell -Command git status"),
            ):
                with self.subTest(command=command):
                    result = classify_claude_pretool_payload(self._hook_payload(tool_name, command), str(repo))
                    self.assertFalse(result["classified"])
                    self.assertIn("read-only", result["reason"])

    def test_ambiguous_or_unsupported_git_commands_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            for tool_name, command in (
                ("Bash", "if git commit"),
                ("Bash", "git ci"),
                ("Bash", "git add README.md"),
                ("Bash", "git branch feature/test"),
                ("Bash", "git branch -D feature/test"),
            ):
                with self.subTest(command=command):
                    with self.assertRaises(HookPayloadError):
                        classify_claude_pretool_payload(self._hook_payload(tool_name, command), str(repo))

    def test_execution_capable_git_read_options_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            for command in (
                "git -c diff.external=echo diff --ext-diff HEAD~1 HEAD",
                "git diff --ext-diff",
                "git show --textconv HEAD:file.txt",
                "GIT_EXTERNAL_DIFF=echo git diff",
            ):
                with self.subTest(command=command):
                    with self.assertRaises(HookPayloadError):
                        classify_claude_pretool_payload(
                            self._hook_payload("Bash", command),
                            str(repo),
                        )

    def test_policy_preflight_response_file_contract_writes_compact_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            request_path = tmp_path / "request.json"
            response_path = tmp_path / "response.json"
            error_path = tmp_path / "error.log"
            request_path.write_text(
                json.dumps(
                    {
                        "request_type": "claude-pretool-hook",
                        "cwd": str(tmp_path),
                        "hook_payload": self._hook_payload("Bash", "git status"),
                    }
                ),
                encoding="utf-8",
            )

            completed = self._run_policy_preflight(
                "--request-file",
                str(request_path),
                "--response-file",
                str(response_path),
                "--error-file",
                str(error_path),
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            response_text = response_path.read_text(encoding="utf-8")
            payload = json.loads(response_text)
            self.assertEqual(
                response_text,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
            self.assertFalse(payload["classified"])
            self.assertEqual(payload["request_type"], "claude-pretool-hook")
            self.assertEqual(error_path.read_text(encoding="utf-8"), "")

    def test_policy_preflight_response_file_captures_malformed_request_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            request_path = tmp_path / "request.json"
            response_path = tmp_path / "response.json"
            error_path = tmp_path / "error.log"
            request_path.write_text("{not-json", encoding="utf-8")

            completed = self._run_policy_preflight(
                "--request-file",
                str(request_path),
                "--response-file",
                str(response_path),
                "--error-file",
                str(error_path),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["decision"], "error")
            self.assertEqual(payload["reason"], "malformed request JSON")
            self.assertEqual(error_path.read_text(encoding="utf-8"), "malformed request JSON")

    def test_policy_preflight_stdin_stdout_contract_remains_available(self):
        completed = self._run_policy_preflight(
            input_text=json.dumps(
                {
                    "request_type": "claude-pretool-hook",
                    "cwd": str(ROOT),
                    "hook_payload": self._hook_payload("Bash", "git status"),
                }
            )
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["classified"])
        self.assertEqual(payload["request_type"], "claude-pretool-hook")

    def test_rendered_claude_wrapper_forwards_to_agent_policy_adapter_and_fails_open(self):
        script = _render_pretool_hook_script()

        self.assertIn("agent-policy-hook", script)
        self.assertIn("'--provider', 'claude-code'", script)
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertIn("$payload | & $backendExe @backendArgs", script)
        self.assertNotIn("exit 2", script)
        self.assertTrue(script.rstrip().endswith("exit 0"))

    def test_rendered_claude_wrapper_warns_on_malformed_and_git_parse_errors(self):
        allowed = self._run_rendered_wrapper(json.dumps(self._hook_payload("Bash", "git status")))
        malformed = self._run_rendered_wrapper("not-json")
        unsupported = self._run_rendered_wrapper(json.dumps(self._hook_payload("Bash", "git ci")))

        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(allowed.stderr, "")
        # Warnings must reach the model through stdout additionalContext, never stderr:
        # Claude Code discards a hook's stderr when the hook exits 0.
        self.assertEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stderr, "")
        self.assertIn(
            "malformed Claude hook payload JSON",
            json.loads(malformed.stdout)["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(unsupported.returncode, 0)
        self.assertEqual(unsupported.stderr, "")
        self.assertIn(
            "git subcommand 'ci'",
            json.loads(unsupported.stdout)["hookSpecificOutput"]["additionalContext"],
        )


if __name__ == "__main__":
    unittest.main()
