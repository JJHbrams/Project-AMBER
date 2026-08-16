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

from core.context.directives import add_directive
from core.integrations import engram_bootstrap
from core.integrations.policy_preflight import HookPayloadError, classify_claude_pretool_payload
from core.storage.db import get_connection, initialize_db


class ClaudePolicyHookTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return str(result.stdout or "").strip()

    def _run_powershell_file(
        self,
        script_path: Path,
        *,
        cwd: Path,
        input_text: str,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            cwd=cwd,
            input=input_text,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
            check=False,
        )

    def _init_repo(self, root: Path, branch: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Claude Policy Hook Test")
        (root / "README.md").write_text("init\n", encoding="utf-8")
        self._git(root, "add", "README.md")
        self._git(root, "commit", "-m", "init")
        self._git(root, "checkout", "-B", branch)
        return root

    def _init_bare_repo(self, root: Path) -> Path:
        root.parent.mkdir(parents=True, exist_ok=True)
        self._git(root.parent, "init", "--bare", root.name)
        return root

    def _seed_protected_branch_guard(self, db_dir: Path) -> None:
        with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}, clear=False):
            initialize_db(db_dir)
            with patch("core.context.directives.get_connection", side_effect=lambda: get_connection(db_dir)):
                add_directive(
                    "protected-branch-guard",
                    "protect repo writes",
                    source="test",
                    trigger_type="always",
                    enforcement_level="blocking",
                    guard_id="protected-branch",
                    trigger_data={
                        "match": "always",
                        "action_modes_any": ["repo-write"],
                        "render_in_prompt": False,
                    },
                )

    def _run_policy_role(
        self,
        payload_text: str,
        *,
        env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "engram_overlay_entry.py"), "--role", "policy-preflight"],
            cwd=cwd,
            input=payload_text,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
            check=False,
        )

    def test_cli_exit_codes_allow_blocked_and_error(self):
        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_db:
            home = Path(tmp_home)
            db_dir = Path(tmp_db)
            repo_allow = self._init_repo(home / "repo-allow", "feat/allow")
            repo_block = self._init_repo(home / "repo-block", "dev")
            self._seed_protected_branch_guard(db_dir)

            env = os.environ.copy()
            env["ENGRAM_DB_DIR"] = str(db_dir)
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)

            payload = {
                "caller": "claude-code",
                "action": "claude-pretool",
                "cwd": str(repo_allow),
                "action_metadata": {
                    "mode": "repo-write",
                    "category": "claude-pretool",
                    "tags": ["claude-pretool", "write"],
                },
                "execute_guards": True,
                "persist_audit": False,
            }
            allowed = self._run_policy_role(
                json.dumps(payload, ensure_ascii=False),
                env=env,
                cwd=repo_allow,
            )
            blocked_payload = dict(payload, cwd=str(repo_block))
            blocked = self._run_policy_role(
                json.dumps(blocked_payload, ensure_ascii=False),
                env=env,
                cwd=repo_block,
            )
            malformed = self._run_policy_role("{not-json", env=env, cwd=repo_allow)

        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(json.loads(allowed.stdout)["decision"], "allow")
        self.assertEqual(allowed.stderr.strip(), "")

        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(json.loads(blocked.stdout)["decision"], "blocked")
        self.assertIn("protected branch", blocked.stderr.lower())

        self.assertEqual(malformed.returncode, 1)
        self.assertEqual(json.loads(malformed.stdout)["decision"], "error")
        self.assertIn("malformed request json", malformed.stderr.lower())

    def test_hook_payload_classifies_only_repo_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "feat/classify")
            outside = base / "outside"
            outside.mkdir(parents=True, exist_ok=True)
            repo_nested = outside / "nested"
            repo_nested.mkdir(parents=True, exist_ok=True)

            write_result = classify_claude_pretool_payload(
                {"tool_name": "Write", "tool_input": {"file_path": "README.md"}},
                str(repo),
            )
            bash_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "pytest -q && git commit -m test"}},
                str(repo),
            )
            powershell_result = classify_claude_pretool_payload(
                {"tool_name": "PowerShell", "tool_input": {"command": "git commit -m test"}},
                str(repo),
            )
            git_c_result = classify_claude_pretool_payload(
                {"tool_name": "PowerShell", "tool_input": {"command": "git -C ../../repo commit -m test"}},
                str(repo_nested),
            )
            no_pager_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "git --no-pager commit -m test"}},
                str(repo),
            )
            outside_to_repo_result = classify_claude_pretool_payload(
                {"tool_name": "Write", "tool_input": {"file_path": "../repo/README.md"}},
                str(outside),
            )
            read_result = classify_claude_pretool_payload(
                {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
                str(repo),
            )
            shell_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
                str(repo),
            )
            outside_result = classify_claude_pretool_payload(
                {"tool_name": "Edit", "tool_input": {"file_path": "../outside/note.txt"}},
                str(repo),
            )

        self.assertTrue(write_result["classified"])
        self.assertEqual(write_result["request"]["action_metadata"]["mode"], "repo-write")
        self.assertIn("write", write_result["request"]["action_metadata"]["tags"])

        self.assertTrue(bash_result["classified"])
        self.assertIn("git-commit", bash_result["request"]["action_metadata"]["tags"])
        self.assertEqual(bash_result["hook"]["git_write_subcommand"], "commit")

        self.assertTrue(powershell_result["classified"])
        self.assertIn("powershell", powershell_result["request"]["action_metadata"]["tags"])
        self.assertEqual(powershell_result["hook"]["git_write_subcommand"], "commit")

        self.assertTrue(git_c_result["classified"])
        self.assertEqual(git_c_result["hook"]["git_command_cwd"], str(repo.resolve()))
        self.assertEqual(git_c_result["hook"]["git_worktree_root"], str(repo.resolve()))
        self.assertEqual(git_c_result["request"]["cwd"], str(repo.resolve()))

        self.assertTrue(no_pager_result["classified"])
        self.assertEqual(no_pager_result["hook"]["git_write_subcommand"], "commit")

        self.assertTrue(outside_to_repo_result["classified"])
        self.assertEqual(outside_to_repo_result["hook"]["git_worktree_root"], str(repo.resolve()))
        self.assertEqual(outside_to_repo_result["request"]["cwd"], str(repo.resolve()))

        self.assertFalse(read_result["classified"])
        self.assertFalse(shell_result["classified"])
        self.assertFalse(outside_result["classified"])

    def test_hook_payload_tracks_shell_context_prefixes_and_git_repo_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "feat/shell-context")
            repo_with_space = self._init_repo(base / "repo space", "feat/powershell-space")
            outside = base / "outside"
            outside.mkdir(parents=True, exist_ok=True)

            repo_path = str(repo.resolve())
            repo_git_dir = str((repo / ".git").resolve())
            repo_with_space_path = str(repo_with_space.resolve())
            repo_with_space_git_dir = str((repo_with_space / ".git").resolve())

            powershell_cd_result = classify_claude_pretool_payload(
                {"tool_name": "PowerShell", "tool_input": {"command": f"cd {repo_path}; git commit -m test"}},
                str(outside),
            )
            powershell_set_location_result = classify_claude_pretool_payload(
                {
                    "tool_name": "PowerShell",
                    "tool_input": {"command": f"Set-Location '{repo_with_space_path}'; git commit -m test"},
                },
                str(outside),
            )
            powershell_invoke_result = classify_claude_pretool_payload(
                {"tool_name": "PowerShell", "tool_input": {"command": "& git commit -m test"}},
                str(repo),
            )
            powershell_env_result = classify_claude_pretool_payload(
                {
                    "tool_name": "PowerShell",
                    "tool_input": {
                        "command": "$env:GIT_DIR = '.git'; $env:GIT_WORK_TREE = '.'; git commit -m test"
                    },
                },
                str(repo),
            )
            bash_cd_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "cd ../repo && git commit -m test"}},
                str(outside),
            )
            bash_name_value_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "TRACE=1 git commit -m test"}},
                str(repo),
            )
            bash_env_result = classify_claude_pretool_payload(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "env --ignore-environment TRACE=1 git commit -m test"},
                },
                str(repo),
            )
            bash_command_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "command git commit -m test"}},
                str(repo),
            )
            git_dir_only_result = classify_claude_pretool_payload(
                {"tool_name": "Bash", "tool_input": {"command": "git --git-dir=.git commit -m test"}},
                str(repo),
            )
            git_dir_work_tree_result = classify_claude_pretool_payload(
                {
                    "tool_name": "PowerShell",
                    "tool_input": {
                        "command": (
                            f"git --git-dir '{repo_with_space_git_dir}' "
                            f"--work-tree '{repo_with_space_path}' commit -m test"
                        )
                    },
                },
                str(outside),
            )
            git_dir_work_tree_inline_result = classify_claude_pretool_payload(
                {
                    "tool_name": "PowerShell",
                    "tool_input": {
                        "command": f"git --git-dir={repo_git_dir} --work-tree={repo_path} commit -m test"
                    },
                },
                str(outside),
            )

        for result in (
            powershell_cd_result,
            powershell_set_location_result,
            powershell_invoke_result,
            powershell_env_result,
            bash_cd_result,
            bash_name_value_result,
            bash_env_result,
            bash_command_result,
            git_dir_only_result,
            git_dir_work_tree_result,
            git_dir_work_tree_inline_result,
        ):
            self.assertTrue(result["classified"])
            self.assertEqual(result["hook"]["git_write_subcommand"], "commit")

        self.assertEqual(powershell_cd_result["hook"]["git_command_cwd"], repo_path)
        self.assertEqual(powershell_cd_result["hook"]["git_worktree_root"], repo_path)

        self.assertEqual(powershell_set_location_result["hook"]["git_command_cwd"], repo_with_space_path)
        self.assertEqual(powershell_set_location_result["request"]["cwd"], repo_with_space_path)

        self.assertEqual(powershell_env_result["hook"]["git_worktree_root"], repo_path)
        self.assertEqual(powershell_env_result["request"]["cwd"], repo_path)

        self.assertEqual(bash_cd_result["hook"]["git_command_cwd"], repo_path)
        self.assertEqual(bash_name_value_result["hook"]["git_worktree_root"], repo_path)
        self.assertEqual(bash_env_result["hook"]["git_worktree_root"], repo_path)
        self.assertEqual(bash_command_result["hook"]["git_worktree_root"], repo_path)

        self.assertEqual(git_dir_only_result["hook"]["git_worktree_root"], repo_path)
        self.assertEqual(git_dir_only_result["request"]["cwd"], repo_path)

        self.assertEqual(git_dir_work_tree_result["hook"]["git_worktree_root"], repo_with_space_path)
        self.assertEqual(git_dir_work_tree_result["request"]["cwd"], repo_with_space_path)

        self.assertEqual(git_dir_work_tree_inline_result["hook"]["git_worktree_root"], repo_path)
        self.assertEqual(git_dir_work_tree_inline_result["request"]["cwd"], repo_path)

    def test_hook_payload_fail_closes_on_unresolved_or_multiple_git_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "feat/fail-closed")
            bare_repo = self._init_bare_repo(base / "bare.git")

            with self.assertRaisesRegex(HookPayloadError, "unambiguous work-tree"):
                classify_claude_pretool_payload(
                    {
                        "tool_name": "PowerShell",
                        "tool_input": {"command": f"git --git-dir '{str(bare_repo.resolve())}' commit -m test"},
                    },
                    str(base),
                )

            with self.assertRaisesRegex(HookPayloadError, "multiple Git commands"):
                classify_claude_pretool_payload(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": "git status && git commit -m test && git merge main"},
                    },
                    str(repo),
                )

            with self.assertRaisesRegex(HookPayloadError, "git worktree"):
                classify_claude_pretool_payload(
                    {"tool_name": "Bash", "tool_input": {"command": "cd .. && git commit -m test"}},
                    str(repo),
                )

    def test_hook_script_warns_on_malformed_payload_and_uses_frozen_role_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script_path = base / "engram-claude-pretool-hook.ps1"
            script_path.write_text(engram_bootstrap._render_claude_pretool_hook_script(), encoding="utf-8")

            env = os.environ.copy()
            env["USERPROFILE"] = str(base)
            env["HOME"] = str(base)

            malformed = self._run_powershell_file(
                script_path,
                cwd=base,
                input_text="{bad-json",
                env=env,
            )

            with patch.object(engram_bootstrap.sys, "frozen", True, create=True), patch.object(
                engram_bootstrap.sys,
                "executable",
                r"C:\EngramOverlay\dist\engram-overlay\engram-overlay.exe",
            ):
                frozen_script = engram_bootstrap._render_claude_pretool_hook_script()

        self.assertEqual(malformed.returncode, 0)
        self.assertIn("malformed claude hook payload json", malformed.stderr.lower())
        self.assertIn("policy guidance", malformed.stderr.lower())
        self.assertEqual(malformed.stdout.strip(), "")
        self.assertIn("engram-overlay.exe", frozen_script)
        self.assertIn("agent-policy-hook", frozen_script)
        self.assertNotIn("engram_overlay_entry.py", frozen_script)
        self.assertIn("--provider", frozen_script)
        self.assertIn("claude-code", frozen_script)
        self.assertNotIn("Out-Null", frozen_script)

    def test_hook_script_forwards_denial_json_and_backend_failures_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            backend_script = base / "mock_policy_backend.py"
            backend_script.write_text(
                "\n".join(
                    [
                        "import argparse",
                        "import json",
                        "import os",
                        "import sys",
                        "sys.stdin.read()",
                        "payload = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': os.environ['MOCK_POLICY_REASON']}}, separators=(',', ':'))",
                        "print(payload) if os.environ['MOCK_POLICY_EXIT_CODE'] == '0' else print(os.environ['MOCK_POLICY_REASON'], file=sys.stderr)",
                        "raise SystemExit(int(os.environ['MOCK_POLICY_EXIT_CODE']))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                engram_bootstrap,
                "_agent_policy_hook_command_parts",
                return_value=(sys.executable, [str(backend_script)]),
            ):
                script_path = base / "engram-claude-pretool-hook.ps1"
                script_path.write_text(engram_bootstrap._render_claude_pretool_hook_script(), encoding="utf-8")

            env = os.environ.copy()
            env["USERPROFILE"] = str(base)
            env["HOME"] = str(base)

            blocked = self._run_powershell_file(
                script_path,
                cwd=base,
                input_text='{"tool_name":"Read"}',
                env={
                    **env,
                    "MOCK_POLICY_REASON": "blocked by test",
                    "MOCK_POLICY_EXIT_CODE": "0",
                },
            )
            errored = self._run_powershell_file(
                script_path,
                cwd=base,
                input_text='{"tool_name":"Read"}',
                env={
                    **env,
                    "MOCK_POLICY_REASON": "backend exploded",
                    "MOCK_POLICY_EXIT_CODE": "1",
                },
            )

        self.assertEqual(blocked.returncode, 0)
        self.assertEqual(json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(blocked.stderr.strip(), "")

        self.assertEqual(errored.returncode, 0)
        self.assertEqual(errored.stdout.strip(), "")
        self.assertIn("backend exploded", errored.stderr)
        self.assertIn("guidance unavailable", errored.stderr)

    def test_hook_sync_is_idempotent_and_preserves_user_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            engram_dir = home / ".engram"
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "hooks": {
                            "SessionStart": [{"hooks": [{"type": "command", "command": "user-session"}]}],
                            "PreToolUse": [{"hooks": [{"type": "command", "command": "user-pretool"}]}],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            session_script = engram_dir / "engram-sessionstart-hook.ps1"
            pretool_script = engram_dir / "engram-claude-pretool-hook.ps1"
            with patch.object(engram_bootstrap, "_ENGRAM_DIR", engram_dir), patch.object(
                engram_bootstrap, "_CLAUDE_SETTINGS_PATH", settings_path
            ), patch.object(engram_bootstrap, "_SESSIONSTART_HOOK_SCRIPT_PATH", session_script), patch.object(
                engram_bootstrap,
                "_PRETOOL_HOOK_SCRIPT_PATH",
                pretool_script,
            ):
                engram_bootstrap.sync_sessionstart_hook(True)
                engram_bootstrap.sync_claude_pretool_hook(True)
                engram_bootstrap.sync_claude_pretool_hook(True)

                enabled = json.loads(settings_path.read_text(encoding="utf-8"))
                session_script_exists = session_script.exists()
                pretool_script_exists = pretool_script.exists()
                engram_bootstrap.sync_claude_pretool_hook(False)
                disabled = json.loads(settings_path.read_text(encoding="utf-8"))
                pretool_script_removed = not pretool_script.exists()

        pretool_entries = enabled["hooks"]["PreToolUse"]
        session_entries = enabled["hooks"]["SessionStart"]
        self.assertEqual(
            sum(
                1
                for entry in pretool_entries
                for hook in entry.get("hooks", [])
                if "engram-claude-pretool-hook" in str(hook.get("command", ""))
            ),
            1,
        )
        self.assertTrue(any("user-pretool" in str(hook.get("command", "")) for entry in pretool_entries for hook in entry.get("hooks", [])))
        self.assertTrue(any("user-session" in str(hook.get("command", "")) for entry in session_entries for hook in entry.get("hooks", [])))
        self.assertTrue(session_script_exists)
        self.assertTrue(pretool_script_exists)

        self.assertFalse(any("engram-claude-pretool-hook" in str(hook.get("command", "")) for entry in disabled["hooks"]["PreToolUse"] for hook in entry.get("hooks", [])))
        self.assertTrue(any("user-pretool" in str(hook.get("command", "")) for entry in disabled["hooks"]["PreToolUse"] for hook in entry.get("hooks", [])))
        self.assertTrue(pretool_script_removed)

    def test_hook_sync_preserves_mixed_matcher_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            engram_dir = home / ".engram"
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "matcher": "workspace",
                                    "hooks": [
                                        {"type": "command", "command": "user-session"},
                                        {"type": "command", "command": "powershell engram-sessionstart-hook.ps1"},
                                    ],
                                },
                                {
                                    "matcher": "engram-only",
                                    "hooks": [
                                        {"type": "command", "command": "powershell engram-sessionstart-hook.ps1"},
                                    ],
                                },
                            ],
                            "PreToolUse": [
                                {
                                    "matcher": "*.py",
                                    "hooks": [
                                        {"type": "command", "command": "user-pretool"},
                                        {"type": "command", "command": "powershell engram-claude-pretool-hook.ps1"},
                                    ],
                                },
                                {
                                    "matcher": "engram-only",
                                    "hooks": [
                                        {"type": "command", "command": "powershell engram-claude-pretool-hook.ps1"},
                                    ],
                                },
                            ],
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            session_script = engram_dir / "engram-sessionstart-hook.ps1"
            pretool_script = engram_dir / "engram-claude-pretool-hook.ps1"
            with patch.object(engram_bootstrap, "_ENGRAM_DIR", engram_dir), patch.object(
                engram_bootstrap, "_CLAUDE_SETTINGS_PATH", settings_path
            ), patch.object(engram_bootstrap, "_SESSIONSTART_HOOK_SCRIPT_PATH", session_script), patch.object(
                engram_bootstrap,
                "_PRETOOL_HOOK_SCRIPT_PATH",
                pretool_script,
            ):
                engram_bootstrap.sync_sessionstart_hook(True)
                engram_bootstrap.sync_claude_pretool_hook(True)
                enabled = json.loads(settings_path.read_text(encoding="utf-8"))
                engram_bootstrap.sync_sessionstart_hook(False)
                engram_bootstrap.sync_claude_pretool_hook(False)
                disabled = json.loads(settings_path.read_text(encoding="utf-8"))

        enabled_session_group = next(
            entry for entry in enabled["hooks"]["SessionStart"] if entry.get("matcher") == "workspace"
        )
        enabled_pretool_group = next(
            entry for entry in enabled["hooks"]["PreToolUse"] if entry.get("matcher") == "*.py"
        )
        self.assertEqual(
            [hook["command"] for hook in enabled_session_group["hooks"]],
            ["user-session"],
        )
        self.assertEqual(
            [hook["command"] for hook in enabled_pretool_group["hooks"]],
            ["user-pretool"],
        )
        self.assertFalse(any(entry.get("matcher") == "engram-only" for entry in enabled["hooks"]["SessionStart"]))
        self.assertFalse(any(entry.get("matcher") == "engram-only" for entry in enabled["hooks"]["PreToolUse"]))
        self.assertEqual(
            sum(
                1
                for entry in enabled["hooks"]["PreToolUse"]
                for hook in entry.get("hooks", [])
                if "engram-claude-pretool-hook" in str(hook.get("command", ""))
            ),
            1,
        )

        disabled_session_group = next(
            entry for entry in disabled["hooks"]["SessionStart"] if entry.get("matcher") == "workspace"
        )
        disabled_pretool_group = next(
            entry for entry in disabled["hooks"]["PreToolUse"] if entry.get("matcher") == "*.py"
        )
        self.assertEqual([hook["command"] for hook in disabled_session_group["hooks"]], ["user-session"])
        self.assertEqual([hook["command"] for hook in disabled_pretool_group["hooks"]], ["user-pretool"])
        self.assertFalse(
            any(
                "engram-sessionstart-hook" in str(hook.get("command", ""))
                for entry in disabled["hooks"]["SessionStart"]
                for hook in entry.get("hooks", [])
            )
        )
        self.assertFalse(
            any(
                "engram-claude-pretool-hook" in str(hook.get("command", ""))
                for entry in disabled["hooks"]["PreToolUse"]
                for hook in entry.get("hooks", [])
            )
        )

    def test_codex_hook_sync_is_idempotent_and_preserves_user_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / ".codex" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "^Bash$",
                                    "hooks": [{"type": "command", "command": "user-policy"}],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(engram_bootstrap, "_CODEX_HOOKS_PATH", hooks_path):
                first = engram_bootstrap.sync_codex_pretool_hook(True)
                second = engram_bootstrap.sync_codex_pretool_hook(True)

                self.assertTrue(first["ok"])
                self.assertTrue(first["trust_required"])
                self.assertFalse(second["changed"])
                enabled = json.loads(hooks_path.read_text(encoding="utf-8"))
                commands = [
                    hook.get("command", "")
                    for entry in enabled["hooks"]["PreToolUse"]
                    for hook in entry.get("hooks", [])
                ]
                self.assertIn("user-policy", commands)
                self.assertEqual(sum("engram-codex-pretool-hook" in item for item in commands), 1)

                disabled = engram_bootstrap.sync_codex_pretool_hook(False)

            self.assertTrue(disabled["ok"])
            remaining = json.loads(hooks_path.read_text(encoding="utf-8"))
            remaining_commands = [
                hook.get("command", "")
                for entry in remaining["hooks"]["PreToolUse"]
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(remaining_commands, ["user-policy"])

    def test_posix_claude_wrapper_is_fail_open(self):
        script = engram_bootstrap._render_claude_pretool_hook_posix_script()

        self.assertIn("policy-guidance.disabled", script)
        self.assertIn("agent-policy-hook", script)
        self.assertIn("--provider", script)
        self.assertTrue(script.rstrip().endswith("exit 0"))
        self.assertNotIn("exit 1", script)


if __name__ == "__main__":
    unittest.main()
