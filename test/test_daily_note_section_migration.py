"""옛 daily note -> 프로젝트 섹션 구조 마이그레이션 회귀.

옛 구조는 시각이 바깥 축이고 제목에 프로젝트 라벨을 접어 넣었다("외 N개").
접힌 것은 복원할 수 없으므로 본문의 ``- 프로젝트:`` 줄을 먼저 읽어야 한다.

옛 항목은 링크 자리에 **노드 id** 를 적었다. 그대로 헤딩으로 쓰면 같은
프로젝트가 옛 기록과 새 기록에서 다른 이름으로 갈린다.
"""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "daily_migration",
    Path(__file__).resolve().parents[1] / "scripts" / "dev"
    / "migrate_daily_notes_to_project_sections.py",
)
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)

_NODES = {
    "session-agent-orchestration": "session-agent-orchestration-overview",
    "claude-image-forge": "claude-image-forge-overview",
    "truviewcadmom": "truviewcadmom",
}
_MAP = {"projectintelcontunuum": "project-amber-project-engram"}

OLD = """---
id: daily-2026-09-18
title: 2026-09-18 Daily Checkpoints
---

# 2026-09-18

## Engram 자동 체크포인트

<!-- engram-checkpoint:cp-1 -->
### 17:02 — claude-image-forge
- 요약: 이미지 모듈
- 프로젝트: claude-image-forge

<!-- engram-checkpoint:cp-2 -->
### 11:00 — project-amber-project-engram
- 요약: 엔그램 작업
- 다음 작업: 후속
- 프로젝트: [[project-amber-project-engram]]

<!-- engram-checkpoint:cp-3 -->
### 09:15 — session-agent-orchestration, projectintelcontunuum 외 2개
- 요약: 섞인 구간
- 프로젝트: session-agent-orchestration, projectintelcontunuum, truviewcadmom
"""


def _convert(text=OLD):
    def resolve(key):
        if key in _MAP:
            return _MAP[key]
        return _NODES.get(key)

    with mock.patch.object(migration, "resolve_kg_node_id", side_effect=resolve), \
         mock.patch.object(migration, "get_cfg_value", return_value=_MAP):
        return migration.convert(text)


class SectionStructureTests(unittest.TestCase):
    def setUp(self):
        self.out = _convert()
        self.lines = self.out.splitlines()

    def test_projects_become_the_outer_headings(self):
        self.assertEqual(
            [line for line in self.lines if line.startswith("# ")],
            ["# claude-image-forge", "# projectintelcontunuum", "# session-agent-orchestration"],
        )

    def test_times_become_the_inner_headings(self):
        self.assertEqual(
            [line for line in self.lines if line.startswith("## ")],
            ["## 17:02", "## 11:00", "## 09:15"],
        )

    def test_the_date_heading_and_group_title_are_dropped(self):
        self.assertNotIn("# 2026-09-18", self.out)
        self.assertNotIn("Engram 자동 체크포인트", self.out)

    def test_frontmatter_is_preserved(self):
        self.assertTrue(self.out.startswith("---\nid: daily-2026-09-18\n"))

    def test_bodies_survive_intact(self):
        self.assertIn("- 요약: 엔그램 작업", self.out)
        self.assertIn("- 다음 작업: 후속", self.out)

    def test_checkpoint_markers_survive(self):
        for marker in ("cp-1", "cp-2", "cp-3"):
            self.assertIn(f"<!-- engram-checkpoint:{marker} -->", self.out)

    def test_spacing_is_uniform(self):
        self.assertNotIn("\n\n\n", self.out)


class HeadingNameTests(unittest.TestCase):
    def test_a_node_id_label_is_mapped_back_to_the_project_name(self):
        """옛 라벨은 노드 id 다. 새 기록의 이름과 갈리면 섹션이 둘로 쪼개진다."""
        self.assertIn("# projectintelcontunuum", _convert())
        self.assertNotIn("# project-amber-project-engram", _convert())

    def test_an_overview_suffix_is_stripped(self):
        with mock.patch.object(migration, "get_cfg_value", return_value={}):
            self.assertEqual(migration._project_name("[[foo-overview]]"), "foo")

    def test_a_folded_label_is_not_used_when_the_body_lists_projects(self):
        """'외 2개' 는 요약본이라 첫 항목만 신뢰한다 — 본문이 원본이다."""
        self.assertIn("# session-agent-orchestration", _convert())
        self.assertNotIn("외 2개", _convert())


class RelinkTests(unittest.TestCase):
    def test_a_project_missing_its_link_gains_one(self):
        self.assertIn("- 프로젝트: [[claude-image-forge-overview]]", _convert())

    def test_every_project_in_a_mixed_entry_is_linked(self):
        self.assertIn(
            "- 프로젝트: [[session-agent-orchestration-overview]], "
            "[[project-amber-project-engram]], [[truviewcadmom]]",
            _convert(),
        )

    def test_an_unresolved_name_stays_plain(self):
        text = OLD.replace("- 프로젝트: claude-image-forge", "- 프로젝트: unknown-thing")
        self.assertIn("- 프로젝트: unknown-thing", _convert(text))


class NoOpTests(unittest.TestCase):
    def test_a_note_without_old_entries_is_left_alone(self):
        self.assertIsNone(_convert("---\nid: x\n---\n\n# projA\n\n## 12:30\n- 요약: 이미 새 구조\n"))

    def test_an_empty_note_is_left_alone(self):
        self.assertIsNone(_convert(""))


if __name__ == "__main__":
    unittest.main()
