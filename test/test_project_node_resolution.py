"""project_key -> KG node 해석 회귀.

자동 체크포인트는 여기서 나온 노드의 ``## Progress`` 를 실제로 덮어쓰고 daily
note 에 위키링크로 건다. 그래서 틀린 매칭은 무관한 문서를 오염시킨다.

2026-09-17 실측: daily note 의 프로젝트 링크 34건 중 33건이 잘못된
``kg_node_map`` 항목(하위 설계 문서를 가리킴) 때문에 한 노드로 몰렸고, 나머지
1건은 양방향 prefix 매칭 때문에 프로젝트 인덱스 대신 버그리포트로 갔다.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.context import project_scope


def _fake_conn(node_ids):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kg_nodes (id TEXT, type TEXT, updated_at TEXT)")
    for i, nid in enumerate(node_ids):
        conn.execute(
            "INSERT INTO kg_nodes (id, type, updated_at) VALUES (?, 'project', ?)",
            (nid, f"2026-09-{10 + i:02d}"),
        )
    conn.commit()
    return conn


class ExactMatchOnlyTests(unittest.TestCase):
    NODES = [
        "graph-memory-roadmap",
        "truviewcadmom-mode-collapse-디버깅-기록",
        "truviewcadmom",
        "project-amber-project-engram",
    ]

    def _resolve(self, key, mapping=None):
        with mock.patch.object(project_scope, "get_cfg_value", return_value=mapping or {}), \
             mock.patch.object(project_scope, "get_connection", return_value=_fake_conn(self.NODES)):
            return project_scope.resolve_kg_node_id(key)

    def test_exact_id_resolves(self):
        self.assertEqual(self._resolve("truviewcadmom"), "truviewcadmom")

    def test_digest_suffix_is_stripped_before_matching(self):
        self.assertEqual(
            self._resolve("project-amber-project-engram-a323e1a1"),
            "project-amber-project-engram",
        )

    def test_prefix_no_longer_matches_a_longer_node(self):
        """'truviewcadmom' 이 버그리포트 노드로 새지 않는다."""
        got = self._resolve("truviewcadmom")
        self.assertNotEqual(got, "truviewcadmom-mode-collapse-디버깅-기록")

    def test_unrelated_key_resolves_to_nothing(self):
        """맞는 노드가 없으면 추측하지 않고 None. 호출부가 project_key 를 쓴다."""
        self.assertIsNone(self._resolve("projectintelcontunuum-a323e1a1"))

    def test_partial_key_does_not_match(self):
        self.assertIsNone(self._resolve("graph"))
        self.assertIsNone(self._resolve("truview"))

    def test_general_and_blank_resolve_to_nothing(self):
        for key in ("general", "", "   "):
            with self.subTest(key=key):
                self.assertIsNone(self._resolve(key))

    def test_explicit_mapping_wins(self):
        got = self._resolve(
            "projectintelcontunuum-a323e1a1",
            mapping={"projectintelcontunuum": "project-amber-project-engram"},
        )
        self.assertEqual(got, "project-amber-project-engram")

    def test_separator_differences_are_normalized(self):
        self.assertEqual(self._resolve("graph_memory_roadmap"), "graph-memory-roadmap")


class ShippedMappingTests(unittest.TestCase):
    """config/config.yaml 의 매핑은 프로젝트 대표 노트를 가리켜야 한다."""

    def test_mapping_points_at_project_overview_not_a_child_doc(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
        mapping = cfg["memory"]["scope"].get("kg_node_map") or {}
        self.assertEqual(
            mapping.get("projectintelcontunuum"),
            "project-amber-project-engram",
            "하위 설계 문서를 가리키면 그 문서의 Progress 가 무관한 기록으로 덮인다",
        )


class CheckpointBlockTests(unittest.TestCase):
    """KG 노드가 없어도 프로젝트 이름은 남아야 한다."""

    def _block(self, **kw):
        from core.memory.daily_checkpoint import _checkpoint_block
        from datetime import datetime

        return _checkpoint_block(
            "cp-1", datetime(2026, 9, 17, 14, 3), "요약", "다음", **kw
        )

    def test_linked_when_node_is_known(self):
        out = self._block(project_label="engram", project_node_id="project-amber-project-engram")
        self.assertIn("- 프로젝트: [[project-amber-project-engram]]", out)

    def test_plain_name_when_node_is_unknown(self):
        out = self._block(project_label="projectintelcontunuum", project_node_id="")
        self.assertIn("- 프로젝트: projectintelcontunuum", out)
        self.assertNotIn("[[", out)

    def test_no_project_line_when_nothing_is_known(self):
        out = self._block(project_label="", project_node_id="")
        self.assertNotIn("- 프로젝트:", out)


if __name__ == "__main__":
    unittest.main()
