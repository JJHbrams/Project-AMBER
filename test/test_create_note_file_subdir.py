"""KnowledgeGraph.create_note_file()의 subdir 파라미터 회귀 테스트.

원래 note_type에 경로("projects/foo/bar")를 욱여넣는 트릭만 있었는데, 이게
DB에 기록되는 실제 type을 NODE_TYPES 밖의 값으로 오염시켜 다음 kg_sync 때
"concept"로 강등되는 문제가 있었다. subdir을 note_type과 분리된 파라미터로
추가해 이 문제를 근본적으로 없앤다. 여기서는:
- subdir 미지정 시 기존과 동일하게 동작(하위호환)
- subdir 지정 시 note_type 기본 디렉토리 아래 정확히 중첩 배치되는지
- note_type(및 DB에 기록되는 type)이 subdir와 무관하게 그대로 유지되는지
- 경로 탈출(../) 시도가 막히는지
를 확인한다.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph.knowledge.knowledge_graph import KnowledgeGraph, NODE_TYPE_DIRS


class CreateNoteFileSubdirTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self._tmpdir.name)
        self._db_path = str(self.vault_path / "test_engram.db")

        # get_connection()은 매 호출마다 새 sqlite3.Connection을 여는 게 실제 동작이라
        # 같은 파일을 가리키는 새 커넥션을 매번 만들어 그 패턴을 그대로 흉내낸다.
        def _fake_get_connection():
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            return conn

        self._patcher = patch(
            "core.graph.knowledge.knowledge_graph.get_connection",
            side_effect=_fake_get_connection,
        )
        self._patcher.start()
        self.kg = KnowledgeGraph()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_no_subdir_matches_previous_behavior(self):
        filepath = self.kg.create_note_file(
            "Plain Note", "본문", "concept", [], [], self.vault_path
        )
        expected = self.vault_path / "docs" / NODE_TYPE_DIRS["concept"] / "plain-note.md"
        self.assertEqual(filepath, expected)
        self.assertTrue(filepath.exists())

    def test_subdir_nests_under_note_type_dir_and_type_is_preserved(self):
        filepath = self.kg.create_note_file(
            "Sub Note", "본문", "project", [], [], self.vault_path,
            subdir="001_TruviewCADMOM/log",
        )
        expected = (
            self.vault_path / "docs" / NODE_TYPE_DIRS["project"]
            / "001_TruviewCADMOM" / "log" / "sub-note.md"
        )
        self.assertEqual(filepath, expected)
        self.assertTrue(filepath.exists())

        node = self.kg.get_node(filepath.stem)
        self.assertIsNotNone(node)
        # 옛 트릭(note_type에 경로)과 달리 type이 "project" 그대로 유지돼야 함 —
        # NODE_TYPES 밖 값으로 오염돼 다음 sync 때 "concept"로 강등되던 문제가 없어짐.
        self.assertEqual(node["type"], "project")

    def test_subdir_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.kg.create_note_file(
                "Escape Note", "본문", "concept", [], [], self.vault_path,
                subdir="../../../etc",
            )


if __name__ == "__main__":
    unittest.main()
