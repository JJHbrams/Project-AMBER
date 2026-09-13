"""현자(sage) 승급 경로가 실제로 배선되어 있는지 확인한다.

에이전트 정의만 있고 skill 이 배포되지 않으면 orchestrator 는 승급 기준을 모르고,
skill 만 있고 정의가 배포되지 않으면 호출이 실패한다. 둘은 같이 살아야 한다.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".github" / "skills" / "sage" / "SKILL.md"
AGENTS_ROOT = ROOT / "config" / "agents"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, _ = text.split("---\n", 2)
    return yaml.safe_load(frontmatter)


class SageSkillTests(unittest.TestCase):
    def test_skill_declares_escalation_triggers_and_call_paths(self):
        meta = _frontmatter(SKILL)
        body = SKILL.read_text(encoding="utf-8")

        self.assertEqual(meta["name"], "sage")
        # 사용자는 한국어로 "현자"라 부른다. description 에 없으면 발동하지 않는다.
        for trigger in ("현자", "sage", "PC 제어"):
            self.assertIn(trigger, meta["description"])

        # 대칭 승급: 각 provider 최상위 모델.
        self.assertIn("Fable", body)
        self.assertIn("gpt-6-astra", body)
        # PC 제어는 codex computer_use 로만, 그리고 승인 뒤에만.
        self.assertIn("--enable computer_use", body)
        self.assertIn("[SAGE_ACT]", body)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox\"", body)

    def test_sage_is_read_only_in_every_provider_definition(self):
        claude = _frontmatter(AGENTS_ROOT / "claude" / "sage.md")
        copilot = _frontmatter(AGENTS_ROOT / "copilot" / "sage.agent.md")
        codex = tomllib.loads(
            (AGENTS_ROOT / "codex" / "sage.toml").read_text(encoding="utf-8")
        )

        # 자문 전용 — 쓰기/실행 도구를 쥐면 최상위 모델이 구현 루프를 돌아 비용이 터진다.
        self.assertNotIn("Edit", claude["tools"])
        self.assertNotIn("Bash", claude["tools"])
        self.assertNotIn("edit", copilot["tools"])
        self.assertNotIn("execute", copilot["tools"])
        self.assertEqual(codex["sandbox_mode"], "read-only")

    def test_installers_deploy_the_sage_skill_and_definitions(self):
        helper = (ROOT / "installer" / "deploy_agent_definitions.ps1").read_text(
            encoding="utf-8-sig"
        )
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(
            encoding="utf-8-sig"
        )
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")
        cache = (ROOT / "installer" / "build-cache.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('"planner", "coder", "servant", "sage"', helper)
        for script in (configure, shims):
            self.assertIn('"orchestrate", "sage"', script)
        # The current installer packages the shared skills directory as a
        # recursive set; Sage is included through that rule.
        self.assertIn('Source: "..\\.github\\skills\\*"', iss)
        self.assertIn('Flags: recursesubdirs createallsubdirs ignoreversion', iss)
        self.assertTrue(SKILL.is_file())
        # 캐시 서명에 빠지면 skill 을 고쳐도 installer 가 재빌드되지 않는다.
        self.assertIn(".github\\skills\\sage\\SKILL.md", cache)


if __name__ == "__main__":
    unittest.main()
