from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context.directives import add_directive, list_directive_policy_audit
from core.integrations.git_policy_hook import (
    MANAGED_MARKER,
    OPT_OUT_MARKER_NAME,
    POLICY_LOCK_NAME,
    POLICY_LOCK_STALE_SECONDS,
    POLICY_STATE_NAME,
    POWERSHELL_HOOK_NAME,
    GitHookError,
    ensure_repo_policy,
    git_hook_status,
    install_git_hook,
    uninstall_git_hook,
)
from core.storage.db import get_connection, initialize_db


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable).resolve()


class _GitPolicyHookTestCase(unittest.TestCase):
    def _git(self, repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=merged_env,
            timeout=30,
        )

    def _init_repo(self, path: Path, branch: str = "dev") -> Path:
        path.mkdir(parents=True, exist_ok=True)
        created = self._git(path, "init", "-b", branch)
        if created.returncode != 0:
            self.assertEqual(self._git(path, "init").returncode, 0)
            self.assertEqual(self._git(path, "checkout", "-b", branch).returncode, 0)
        self.assertEqual(self._git(path, "config", "user.email", "engram-test@example.invalid").returncode, 0)
        self.assertEqual(self._git(path, "config", "user.name", "Engram Test").returncode, 0)
        (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.assertEqual(self._git(path, "add", "tracked.txt").returncode, 0)
        self.assertEqual(self._git(path, "commit", "-m", "initial").returncode, 0)
        return path

    def _hooks_dir(self, repo: Path) -> Path:
        return self._common_dir(repo) / "hooks"

    def _common_dir(self, repo: Path) -> Path:
        result = self._git(repo, "rev-parse", "--git-common-dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = repo / common
        return common.resolve()


class GitPolicyHookManagementTests(_GitPolicyHookTestCase):
    def test_install_is_idempotent_and_status_reports_managed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            first = install_git_hook(repo)
            second = install_git_hook(repo)
            status = git_hook_status(repo)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertTrue(status["installed"])
            self.assertTrue(status["active"])
            hooks = self._hooks_dir(repo)
            wrapper = (hooks / "pre-commit").read_text(encoding="utf-8")
            self.assertIn(MANAGED_MARKER, wrapper)
            self.assertIn("policy-guidance.disabled", wrapper)
            self.assertIn("git-hook", wrapper)
            self.assertIn("advise", wrapper)
            self.assertIn("exit 0", wrapper)
            self.assertFalse((hooks / POWERSHELL_HOOK_NAME).exists())

    def test_install_refuses_existing_hook_and_custom_hooks_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "existing")
            hooks = self._hooks_dir(repo)
            hooks.mkdir(parents=True, exist_ok=True)
            (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with self.assertRaises(GitHookError) as existing_error:
                install_git_hook(repo)
            self.assertEqual(existing_error.exception.code, "existing-hook")
            self.assertEqual((hooks / "pre-commit").read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

            custom_repo = self._init_repo(base / "custom")
            self.assertEqual(self._git(custom_repo, "config", "core.hooksPath", "custom-hooks").returncode, 0)
            with self.assertRaises(GitHookError) as custom_error:
                install_git_hook(custom_repo)
            self.assertEqual(custom_error.exception.code, "custom-hooks-path")

    def test_uninstall_removes_only_managed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            install_git_hook(repo)
            hooks = self._hooks_dir(repo)
            unrelated = hooks / "commit-msg"
            unrelated.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            result = uninstall_git_hook(repo)

            self.assertEqual(result["removed"], ["pre-commit"])
            self.assertTrue(unrelated.exists())
            self.assertFalse((hooks / "pre-commit").exists())
            self.assertFalse((hooks / POWERSHELL_HOOK_NAME).exists())

    def test_install_manages_merge_ff_and_uninstall_restores_previous_value_with_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            common = self._common_dir(repo)
            self.assertEqual(self._git(repo, "config", "--local", "merge.ff", "true").returncode, 0)

            installed = install_git_hook(repo)

            self.assertEqual(installed["merge_ff_values"], ["false"])
            self.assertEqual(self._git(repo, "config", "--local", "--get-all", "merge.ff").stdout.strip(), "false")
            self.assertTrue((common / POLICY_STATE_NAME).exists())

            removed = uninstall_git_hook(repo)

            self.assertTrue(removed["merge_ff_restored"])
            self.assertEqual(self._git(repo, "config", "--local", "--get-all", "merge.ff").stdout.strip(), "true")
            self.assertTrue((common / OPT_OUT_MARKER_NAME).exists())
            self.assertFalse((common / POLICY_STATE_NAME).exists())
            skipped = ensure_repo_policy(repo, guidance_level="warn")
            self.assertTrue(skipped["skipped"])
            self.assertEqual(skipped["reason"], "user-opt-out")

            reinstalled = install_git_hook(repo)

            self.assertTrue(reinstalled["installed"])
            self.assertFalse((common / OPT_OUT_MARKER_NAME).exists())

    def test_bootstrap_ensure_is_idempotent_restores_on_off_and_skips_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo")

            first = ensure_repo_policy(repo, guidance_level="warn")
            second = ensure_repo_policy(repo, guidance_level="enforce_agents")
            disabled = ensure_repo_policy(repo, guidance_level="off")

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertTrue(disabled["merge_ff_restored"])
            self.assertEqual(self._git(repo, "config", "--local", "--get-all", "merge.ff").returncode, 1)
            reenabled = ensure_repo_policy(repo, guidance_level="warn")
            self.assertTrue(reenabled["changed"])
            self.assertEqual(self._git(repo, "config", "--local", "--get-all", "merge.ff").stdout.strip(), "false")

            conflict_repo = self._init_repo(base / "conflict")
            conflict_hook = self._hooks_dir(conflict_repo) / "pre-commit"
            conflict_hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            conflict = ensure_repo_policy(conflict_repo, guidance_level="warn")
            self.assertTrue(conflict["skipped"])
            self.assertEqual(conflict["reason"], "existing-hook")
            self.assertEqual(conflict_hook.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_bootstrap_ensure_fails_open_for_non_repo_and_concurrent_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            non_repo = ensure_repo_policy(base, guidance_level="warn")
            self.assertTrue(non_repo["skipped"])
            self.assertEqual(non_repo["reason"], "git-error")

            repo = self._init_repo(base / "repo")
            lock_path = self._common_dir(repo) / POLICY_LOCK_NAME
            lock_path.write_text("busy\n", encoding="utf-8")
            busy = ensure_repo_policy(repo, guidance_level="warn")
            self.assertTrue(busy["skipped"])
            self.assertEqual(busy["reason"], "policy-busy")

            stale_time = time.time() - POLICY_LOCK_STALE_SECONDS - 1
            os.utime(lock_path, (stale_time, stale_time))
            recovered = ensure_repo_policy(repo, guidance_level="warn")
            self.assertTrue(recovered["installed"])
            self.assertFalse(lock_path.exists())

    def test_linked_worktrees_share_policy_state_and_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo")
            linked = base / "linked"
            added = self._git(repo, "worktree", "add", "-b", "feat/linked", str(linked))
            self.assertEqual(added.returncode, 0, added.stderr)

            ensured = ensure_repo_policy(linked, guidance_level="warn")
            main_status = git_hook_status(repo)
            linked_status = git_hook_status(linked)

            self.assertTrue(ensured["installed"])
            self.assertEqual(main_status["common_dir"], linked_status["common_dir"])
            self.assertTrue(main_status["active"])
            uninstall_git_hook(repo)
            self.assertTrue(git_hook_status(linked)["opted_out"])

    def test_cli_role_returns_structured_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            result = subprocess.run(
                [
                    str(PYTHON),
                    str(ROOT / "engram_overlay_entry.py"),
                    "--role",
                    "git-hook",
                    "status",
                    "--repo",
                    str(repo),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["operation"], "status")


class GitPolicyHookIntegrationTests(_GitPolicyHookTestCase):
    def _seed_policy(self, db_dir: Path) -> None:
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

    def _stage_change(self, repo: Path, content: str) -> None:
        (repo / "tracked.txt").write_text(content, encoding="utf-8")
        self.assertEqual(self._git(repo, "add", "tracked.txt").returncode, 0)

    def test_protected_branch_warns_but_all_commits_proceed_and_chore_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "dev")
            db_dir = base / "db"
            env = {
                "ENGRAM_DB_DIR": str(db_dir),
                "HOME": str(base),
                "USERPROFILE": str(base),
            }
            engram_dir = base / ".engram"
            engram_dir.mkdir(parents=True, exist_ok=True)
            (engram_dir / "user.config.yaml").write_text(
                "directives:\n  policy:\n    guidance_level: enforce_agents\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, env):
                self._seed_policy(db_dir)
                install_git_hook(repo)

            self._stage_change(repo, "warned\n")
            warned = self._git(repo, "commit", "-m", "warned", env=env)
            self.assertEqual(warned.returncode, 0, warned.stderr)
            self.assertIn("protected branch", (warned.stderr + warned.stdout).lower())
            self.assertIn("policy guidance", (warned.stderr + warned.stdout).lower())

            self._stage_change(repo, "allowed chore\n")
            allowed_chore = self._git(
                repo,
                "commit",
                "-m",
                "chore: allowed",
                env={**env, "ENGRAM_CHORE_INTENT": "1", "ENGRAM_CHORE_REASON": "dependency refresh"},
            )
            self.assertEqual(allowed_chore.returncode, 0, allowed_chore.stderr)

            self._stage_change(repo, "invalid false\n")
            invalid_false = self._git(
                repo,
                "commit",
                "-m",
                "chore: invalid false",
                env={**env, "ENGRAM_CHORE_INTENT": "false"},
            )
            self.assertEqual(invalid_false.returncode, 0, invalid_false.stderr)
            self.assertIn("policy guidance", (invalid_false.stderr + invalid_false.stdout).lower())

            self.assertEqual(self._git(repo, "checkout", "-b", "feat/hook-pass").returncode, 0)
            self._stage_change(repo, "feature branch\n")
            feature_commit = self._git(repo, "commit", "-m", "feat: allowed", env=env)
            self.assertEqual(feature_commit.returncode, 0, feature_commit.stderr)

            with patch.dict(os.environ, env):
                with patch("core.context.directives.get_connection", side_effect=lambda: get_connection(db_dir)):
                    audits = list_directive_policy_audit(limit=20)
            chore_audits = [item for item in audits if item["chore_intent"].get("summary") == "dependency refresh"]
            self.assertEqual(len(chore_audits), 1)
            self.assertTrue(chore_audits[0]["chore_intent"]["is_chore"])
            advisory_audits = [
                item
                for item in audits
                if item["decision"] == "advisory"
                and any(result.get("status") == "fail" for result in item["executed_guard_results"])
            ]
            self.assertGreaterEqual(len(advisory_audits), 2)
            self.assertTrue(
                all("policy-guidance" in item["action_metadata"].get("tags", []) for item in advisory_audits)
            )

    def test_backend_error_file_reason_warns_without_blocking_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "feat/backend-error")
            fake_backend = base / "fake_backend.py"
            fake_backend.write_text(
                "import argparse\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--request-file')\n"
                "parser.add_argument('--response-file')\n"
                "parser.add_argument('--error-file')\n"
                "args = parser.parse_args()\n"
                "Path(args.error_file).write_text('backend exploded', encoding='utf-8')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            with patch(
                "core.integrations.git_policy_hook._policy_preflight_backend_command_parts",
                return_value=(str(PYTHON), [str(fake_backend)]),
            ):
                install_git_hook(repo)
            self._stage_change(repo, "backend error\n")

            result = self._git(repo, "commit", "-m", "should proceed")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("policy guidance unavailable", (result.stderr + result.stdout).lower())

    def test_missing_backend_never_blocks_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "dev")
            with patch(
                "core.integrations.git_policy_hook._policy_preflight_backend_command_parts",
                return_value=(str(base / "missing-engram-backend"), []),
            ):
                install_git_hook(repo)
            self._stage_change(repo, "missing backend\n")

            result = self._git(repo, "commit", "-m", "must proceed")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("policy guidance unavailable", (result.stderr + result.stdout).lower())

    def test_hung_backend_is_timed_out_without_blocking_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "dev")
            fake_backend = base / "slow_backend.py"
            fake_backend.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            with patch(
                "core.integrations.git_policy_hook._policy_preflight_backend_command_parts",
                return_value=(str(PYTHON), [str(fake_backend)]),
            ):
                install_git_hook(repo)
            self._stage_change(repo, "slow backend\n")

            started = time.monotonic()
            result = self._git(
                repo,
                "commit",
                "-m",
                "timeout advisor",
                env={"ENGRAM_POLICY_GUIDANCE_TIMEOUT_SECONDS": "1"},
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 10)
            self.assertIn("timed out", (result.stderr + result.stdout).lower())

    def test_global_guidance_toggle_disables_installed_repo_advisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "dev")
            db_dir = base / "db"
            engram_dir = base / ".engram"
            engram_dir.mkdir(parents=True, exist_ok=True)
            (engram_dir / "user.config.yaml").write_text(
                "directives:\n  policy:\n    guidance_enabled: false\n",
                encoding="utf-8",
            )
            env = {
                "ENGRAM_DB_DIR": str(db_dir),
                "HOME": str(base),
                "USERPROFILE": str(base),
            }
            with patch.dict(os.environ, env):
                self._seed_policy(db_dir)
                install_git_hook(repo)
            self._stage_change(repo, "guidance disabled\n")

            result = self._git(repo, "commit", "-m", "commit without guidance", env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("policy guidance", (result.stderr + result.stdout).lower())
            with patch.dict(os.environ, env):
                with patch("core.context.directives.get_connection", side_effect=lambda: get_connection(db_dir)):
                    self.assertEqual(list_directive_policy_audit(limit=20), [])

    def test_disabled_marker_skips_backend_process_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = self._init_repo(base / "repo", "dev")
            marker = base / ".engram" / "policy-guidance.disabled"
            marker.parent.mkdir(parents=True)
            marker.write_text("disabled\n", encoding="utf-8")
            invoked = base / "backend-invoked"
            fake_backend = base / "fake_backend.py"
            fake_backend.write_text(
                "from pathlib import Path\n"
                f"Path({str(invoked)!r}).write_text('yes', encoding='utf-8')\n",
                encoding="utf-8",
            )
            with patch(
                "core.integrations.git_policy_hook._policy_preflight_backend_command_parts",
                return_value=(str(PYTHON), [str(fake_backend)]),
            ):
                install_git_hook(repo)
            self._stage_change(repo, "disabled marker\n")

            result = self._git(
                repo,
                "commit",
                "-m",
                "skip advisor backend",
                env={"HOME": str(base), "USERPROFILE": str(base)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(invoked.exists())


if __name__ == "__main__":
    unittest.main()
