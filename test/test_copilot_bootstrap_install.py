import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CopilotBootstrapInstallTests(unittest.TestCase):
    def _extract_cleanup_function(self, script_path: Path) -> str:
        source = script_path.read_text(encoding="utf-8-sig")
        start = source.index("function Remove-EngramManagedClaudeHooks {")
        end = source.index("# ── Uninstall", start)
        return source[start:end].strip()

    def _cleanup_setup(self, settings_text: str) -> tuple[tempfile.TemporaryDirectory, Path, Path, Path, Path]:
        tmp_dir = tempfile.TemporaryDirectory()
        home = Path(tmp_dir.name)
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(settings_text, encoding="utf-8")
        shim_dir = home / ".engram"
        shim_dir.mkdir(parents=True, exist_ok=True)
        session_script = shim_dir / "engram-sessionstart-hook.ps1"
        pretool_script = shim_dir / "engram-claude-pretool-hook.ps1"
        session_script.write_text("placeholder\n", encoding="utf-8")
        pretool_script.write_text("placeholder\n", encoding="utf-8")
        return tmp_dir, home, settings_path, session_script, pretool_script

    def _exercise_cleanup_function(self, script_path: Path) -> tuple[dict, bool, bool]:
        managed_settings = (
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
            + "\n"
        )
        tmp_dir, home, settings_path, session_script, pretool_script = self._cleanup_setup(managed_settings)
        with tmp_dir:
            quoted_home = str(home).replace("'", "''")
            command = "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    f"$homeDir = '{quoted_home}'",
                    "$env:USERPROFILE = $homeDir",
                    "$env:HOME = $homeDir",
                    "$ShimDir = Join-Path $homeDir '.engram'",
                    "$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)",
                    "function Write-Ok { param([string]$m) }",
                    self._extract_cleanup_function(script_path),
                    "Remove-EngramManagedClaudeHooks",
                    "Remove-EngramManagedClaudeHooks",
                ]
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=ROOT,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                raise AssertionError(
                    f"cleanup failed for {script_path.name}\nstdout={completed.stdout}\nstderr={completed.stderr}"
                )
            updated = json.loads(settings_path.read_text(encoding="utf-8"))
            return updated, session_script.exists(), pretool_script.exists()

    def _exercise_cleanup_parse_failure(self, script_path: Path) -> tuple[int, str, bool, bool]:
        tmp_dir, home, _settings_path, session_script, pretool_script = self._cleanup_setup("{not-json\n")
        with tmp_dir:
            quoted_home = str(home).replace("'", "''")
            command = "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    f"$homeDir = '{quoted_home}'",
                    "$env:USERPROFILE = $homeDir",
                    "$env:HOME = $homeDir",
                    "$ShimDir = Join-Path $homeDir '.engram'",
                    "$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)",
                    "function Write-Ok { param([string]$m) }",
                    self._extract_cleanup_function(script_path),
                    "Remove-EngramManagedClaudeHooks",
                ]
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=ROOT,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            return (
                completed.returncode,
                completed.stderr,
                session_script.exists(),
                pretool_script.exists(),
            )

    def test_full_installer_registers_global_instruction_directory(self):
        env_module = (ROOT / "installer" / "modules" / "08_env.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")', env_module)

    def test_frozen_installer_deploys_and_registers_copilot_protocol(self):
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")
        self.assertIn("config\\clients\\copilot.md", configure)
        self.assertIn('SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")', configure)
        self.assertIn('Source: "..\\config\\clients\\copilot.md"', iss)
        self.assertIn('Source: "..\\config\\overlay.yaml"', iss)
        self.assertIn('Source: "..\\config\\config.yaml"', iss)
        for skill_name in (
            "engram",
            "orchestrate",
            "engram-new-session",
            "engram-task-workflow",
            "engram-wiki-workflow",
            "engram-close-session",
        ):
            self.assertIn(f'.github\\skills\\{skill_name}\\SKILL.md"', iss)
            self.assertIn(skill_name, configure)
        self.assertIn("터미널과 CLI 세션을 새로 시작하세요", configure)

    def test_orchestrate_skill_is_packaged_to_all_skill_roots(self):
        skill = (ROOT / ".github" / "skills" / "orchestrate" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(
            encoding="utf-8-sig"
        )
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("[ACCEPTANCE_AUDIT]", skill)
        self.assertIn("UNVERIFIED", skill)
        self.assertIn("critical criterion", skill)
        for script in (shims, configure):
            self.assertIn('"orchestrate"', script)
            for root in (".agents\\skills", ".claude\\skills", ".copilot\\skills"):
                self.assertIn(root, script)

    def test_wrapper_keeps_bootstrap_for_interactive_options(self):
        shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('set `"SKIP_BOOTSTRAP=0`"', shims)
        self.assertIn(
            'else ($EngramCopilotCmd -i `"!ENGRAM_BOOTSTRAP!`" !ARGS!)',
            shims,
        )

    def test_wiki_workflow_rejects_unrelated_context_nodes(self):
        workflow = (
            ROOT / ".github" / "skills" / "engram-wiki-workflow" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "현재 프로젝트 context나 최근 세션에 노출됐다는 이유만으로",
            workflow,
        )
        self.assertIn("active roadmap에 붙이지 말고", workflow)

    def test_task_workflow_isolates_parallel_and_dirty_work(self):
        workflow = (
            ROOT / ".github" / "skills" / "engram-task-workflow" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("독립 branch+worktree", workflow)
        self.assertIn("현재 worktree에 다른 작업의 미커밋 변경", workflow)
        self.assertIn("dirty worktree를 강제 제거하지 않는다", workflow)
        self.assertIn("단순 유지보수", workflow)
        self.assertIn("새 branch/worktree를 만들지 않는다", workflow)
        self.assertIn("AGENTS.md", workflow)

        branch_guide = (
            ROOT / "installer" / "templates" / "protocols" / "_protocol-git-branch-guide.md"
        ).read_text(encoding="utf-8")
        self.assertIn("worktree는 작업 종류가 아니라 격리가 실제로 필요한지", branch_guide)
        self.assertIn("단순 유지보수를 넘는 문서 작업", branch_guide)

    def test_installers_cleanup_only_engram_managed_claude_hooks(self):
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        source_install = (ROOT / "installer" / "install.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("engram-sessionstart-hook", configure)
        self.assertIn("engram-claude-pretool-hook", configure)
        self.assertIn("PreToolUse", configure)
        self.assertIn("engram-sessionstart-hook", source_install)
        self.assertIn("engram-claude-pretool-hook", source_install)
        self.assertIn("PreToolUse", source_install)
        self.assertIn("Remove-EngramManagedCodexHooks", configure)
        self.assertIn("engram-codex-pretool-hook", configure)
        self.assertIn("Remove-EngramManagedCodexHooks", source_install)
        self.assertIn("engram-codex-pretool-hook", source_install)

    def test_installers_preserve_mixed_matcher_groups_while_removing_engram_hooks(self):
        for script_name in ("configure.ps1", "install.ps1"):
            with self.subTest(script=script_name):
                updated, session_script_exists, pretool_script_exists = self._exercise_cleanup_function(
                    ROOT / "installer" / script_name
                )
                session_group = next(
                    entry for entry in updated["hooks"]["SessionStart"] if entry.get("matcher") == "workspace"
                )
                pretool_group = next(
                    entry for entry in updated["hooks"]["PreToolUse"] if entry.get("matcher") == "*.py"
                )
                self.assertEqual([hook["command"] for hook in session_group["hooks"]], ["user-session"])
                self.assertEqual([hook["command"] for hook in pretool_group["hooks"]], ["user-pretool"])
                self.assertFalse(
                    any(
                        entry.get("matcher") == "engram-only"
                        for entry in updated["hooks"]["SessionStart"] + updated["hooks"]["PreToolUse"]
                    )
                )
                self.assertFalse(
                    any(
                        "engram-sessionstart-hook" in str(hook.get("command", ""))
                        or "engram-claude-pretool-hook" in str(hook.get("command", ""))
                        for event_name in ("SessionStart", "PreToolUse")
                        for entry in updated["hooks"][event_name]
                        for hook in entry.get("hooks", [])
                    )
                )
                if script_name == "configure.ps1":
                    self.assertFalse(session_script_exists)
                    self.assertFalse(pretool_script_exists)

    def test_installers_abort_cleanup_when_claude_settings_json_is_unparseable(self):
        for script_name in ("configure.ps1", "install.ps1"):
            with self.subTest(script=script_name):
                returncode, stderr, session_script_exists, pretool_script_exists = self._exercise_cleanup_parse_failure(
                    ROOT / "installer" / script_name
                )
                self.assertNotEqual(returncode, 0)
                self.assertIn("settings.json", stderr)
                self.assertTrue(session_script_exists)
                self.assertTrue(pretool_script_exists)


if __name__ == "__main__":
    unittest.main()
