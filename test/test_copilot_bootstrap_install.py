import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CopilotBootstrapInstallTests(unittest.TestCase):
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
            "engram-new-session",
            "engram-task-workflow",
            "engram-wiki-workflow",
            "engram-close-session",
        ):
            self.assertIn(f'.github\\skills\\{skill_name}\\SKILL.md"', iss)
            self.assertIn(skill_name, configure)
        self.assertIn("터미널과 CLI 세션을 새로 시작하세요", configure)

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


if __name__ == "__main__":
    unittest.main()
