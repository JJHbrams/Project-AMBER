"""KG 다중 볼트 스코핑 회귀 테스트.

kg_nodes.vault_path 컬럼은 처음부터 있었지만 prune_missing / get_path_mtimes /
resolve_links(전체 모드)가 그걸 WHERE 에 쓰지 않았다. 그래서 두 번째 볼트를 붙이는
순간 볼트 A 를 sync 하면:
- prune_missing 이 볼트 B 노드의 상대경로를 A 기준으로 확인하고 없다는 이유로 전부 삭제
- get_path_mtimes 가 볼트를 섞어, 상대경로가 겹치면 "안 바뀐 파일"로 오판해 sync 를 건너뜀
- resolve_links 가 모든 볼트의 links 엣지를 통째로 삭제

여기서는 각 함수가 자기 볼트만 건드리는지, 그리고 표기가 다른 같은 경로를
같은 볼트로 취급하는지(vault_key 정규화)를 확인한다.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph.knowledge.knowledge_graph import (
    KnowledgeGraph,
    initialize_kg_tables,
    vault_key,
)


def _write_note(path: Path, title: str, links=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = [f"title: {title}", f"id: {title.lower().replace(' ', '-')}", "note_type: concept"]
    if links:
        fm.append("links:")
        fm += [f"  - {l}" for l in links]
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n본문\n", encoding="utf-8")
    return path


class VaultScopingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self._db_path = str(self.root / "test_engram.db")

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

        # 상대경로가 정확히 겹치는 두 볼트 — 스코핑이 없으면 서로를 지운다.
        self.vault_a = self.root / "vaultA" / "docs"
        self.vault_b = self.root / "vaultB" / "docs"
        self.file_a = _write_note(self.vault_a / "concepts" / "note.md", "Note A")
        self.file_b = _write_note(self.vault_b / "concepts" / "note.md", "Note B")
        self.kg.sync_file(self.file_a, self.vault_a)
        self.kg.sync_file(self.file_b, self.vault_b)

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _ids(self):
        conn = sqlite3.connect(self._db_path)
        ids = {r[0] for r in conn.execute("SELECT id FROM kg_nodes")}
        conn.close()
        return ids

    def test_prune_missing_leaves_other_vaults_alone(self):
        """볼트 A 를 prune 해도 상대경로가 같은 볼트 B 노드는 살아 있어야 한다."""
        self.assertEqual(self._ids(), {"note-a", "note-b"})
        pruned = self.kg.prune_missing(self.vault_a)
        self.assertEqual(pruned, [])
        self.assertEqual(self._ids(), {"note-a", "note-b"})

    def test_prune_missing_still_removes_own_orphans(self):
        """자기 볼트에서 사라진 파일은 여전히 정리한다 — 필터가 기능을 죽이면 안 된다."""
        self.file_a.unlink()
        pruned = self.kg.prune_missing(self.vault_a)
        self.assertEqual(pruned, ["note-a"])
        self.assertEqual(self._ids(), {"note-b"})

    def test_get_path_mtimes_is_scoped_to_vault(self):
        """볼트별로 자기 노드의 mtime 만 보여야 한다 — 섞이면 증분 sync 가 파일을 건너뛴다."""
        mtimes_a = self.kg.get_path_mtimes(self.vault_a)
        mtimes_b = self.kg.get_path_mtimes(self.vault_b)
        rel = str(Path("concepts") / "note.md")
        self.assertEqual(set(mtimes_a), {rel})
        self.assertEqual(set(mtimes_b), {rel})
        self.assertAlmostEqual(mtimes_a[rel], self.file_a.stat().st_mtime, places=3)
        self.assertAlmostEqual(mtimes_b[rel], self.file_b.stat().st_mtime, places=3)

    def test_resolve_links_full_mode_keeps_other_vault_edges(self):
        """볼트 A 전체 재해석이 볼트 B 의 links 엣지를 지우면 안 된다."""
        _write_note(self.vault_b / "concepts" / "hub.md", "Hub B", links=["Note B"])
        self.kg.sync_file(self.vault_b / "concepts" / "hub.md", self.vault_b)
        self.kg.resolve_links(self.vault_b)
        self.assertTrue(self._edge_exists("hub-b", "note-b"))

        self.kg.resolve_links(self.vault_a)
        self.assertTrue(self._edge_exists("hub-b", "note-b"))

    def _edge_exists(self, from_id: str, to_id: str) -> bool:
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT 1 FROM kg_edges WHERE from_id=? AND to_id=? AND rel_type='links'",
            (from_id, to_id),
        ).fetchone()
        conn.close()
        return row is not None

    def test_same_vault_written_differently_is_one_vault(self):
        """표기만 다른 같은 경로가 다른 볼트로 갈리면 sync 가 서로를 지운다."""
        alias = self.vault_a.parent / "." / "docs"
        self.assertEqual(vault_key(alias), vault_key(self.vault_a))
        self.assertEqual(set(self.kg.get_path_mtimes(alias)), {str(Path("concepts") / "note.md")})
        self.assertEqual(self.kg.prune_missing(alias), [])

    def test_migration_normalizes_and_backfills_single_vault(self):
        """옛 DB 의 비정규 표기·빈 vault_path 가 단일 볼트일 때 그 볼트로 수렴한다."""
        conn = sqlite3.connect(self._db_path)
        with conn:
            conn.execute("DELETE FROM kg_nodes WHERE id='note-b'")
            conn.execute(
                "UPDATE kg_nodes SET vault_path=? WHERE id='note-a'",
                (str(self.vault_a) + "\\",),
            )
            conn.execute(
                "INSERT INTO kg_nodes (id, title, path, type, vault_path) VALUES "
                "('legacy', 'Legacy', 'concepts/legacy.md', 'concept', '')"
            )
        conn.close()

        initialize_kg_tables()

        conn = sqlite3.connect(self._db_path)
        vaults = {r[0] for r in conn.execute("SELECT DISTINCT vault_path FROM kg_nodes")}
        conn.close()
        self.assertEqual(vaults, {vault_key(self.vault_a)})

    def test_migration_leaves_empty_vault_path_alone_when_ambiguous(self):
        """볼트가 여럿이면 빈 vault_path 를 아무 볼트에 귀속시켜선 안 된다."""
        conn = sqlite3.connect(self._db_path)
        with conn:
            conn.execute(
                "INSERT INTO kg_nodes (id, title, path, type, vault_path) VALUES "
                "('legacy', 'Legacy', 'concepts/legacy.md', 'concept', '')"
            )
        conn.close()

        initialize_kg_tables()

        conn = sqlite3.connect(self._db_path)
        row = conn.execute("SELECT vault_path FROM kg_nodes WHERE id='legacy'").fetchone()
        conn.close()
        self.assertEqual(row[0], "")


if __name__ == "__main__":
    unittest.main()
