"""frontmatter relations → 타입 엣지, 파일명·경로 링크 해석 회귀 테스트.

- `relations: [{to, rel, context}]` 가 rel_type 그대로 엣지가 되고, 재sync 때 교체된다.
- kg_link_nodes 로 넣은 수동 엣지는 재sync 에도 남는다.
- `[[2026-09-29#^event-iran]]` 처럼 frontmatter id 와 다른 파일명 링크도 엣지가 된다
  (이전에는 id/title 조회만 해서 날짜별 daily 문서 사이 엣지가 0개였다).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph.knowledge.knowledge_graph import KnowledgeGraph, parse_markdown


def _write(path: Path, fm: str, body: str = "본문\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm.strip()}\n---\n\n{body}", encoding="utf-8")
    return path


class TypedRelationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._db_path = str(root / "test_engram.db")

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
        self.vault = root / "docs"

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _sync(self, *paths: Path):
        for p in paths:
            self.kg.sync_file(p, self.vault)
        self.kg.resolve_links(self.vault)

    def _edges(self):
        conn = sqlite3.connect(self._db_path)
        rows = {(r[0], r[1], r[2]) for r in conn.execute("SELECT from_id, to_id, rel_type FROM kg_edges")}
        conn.close()
        return rows

    def test_parse_relations_drops_invalid_entries(self):
        parsed = parse_markdown(
            "---\ntitle: A\nrelations:\n"
            "  - {to: b, rel: follows, context: 다음 국면}\n"
            "  - {to: c, rel: not-a-type}\n"
            "  - {to: d, rel: links}\n"
            "  - just-a-string\n---\n본문\n"
        )
        self.assertEqual(parsed["relations"], [{"to": "b", "rel": "follows", "context": "다음 국면"}])

    def test_relations_become_typed_edges_and_are_replaced_on_resync(self):
        ev = _write(self.vault / "ontology" / "events" / "event-iran.md", "id: event-iran\ntitle: 미-이란")
        obs = _write(
            self.vault / "ontology" / "obs" / "obs-iran-2026-10-01.md",
            "id: obs-iran-2026-10-01\ntitle: 이란 10/01\nrelations:\n  - {to: event-iran, rel: part_of}",
        )
        self._sync(ev, obs)
        self.assertIn(("obs-iran-2026-10-01", "event-iran", "part_of"), self._edges())

        _write(
            obs,
            "id: obs-iran-2026-10-01\ntitle: 이란 10/01\nrelations:\n  - {to: event-iran, rel: supports}",
        )
        self._sync(obs)
        edges = self._edges()
        self.assertIn(("obs-iran-2026-10-01", "event-iran", "supports"), edges)
        self.assertNotIn(("obs-iran-2026-10-01", "event-iran", "part_of"), edges)

    def test_manual_edges_survive_resync(self):
        a = _write(self.vault / "concepts" / "a.md", "id: a\ntitle: A")
        b = _write(self.vault / "concepts" / "b.md", "id: b\ntitle: B")
        self._sync(a, b)
        self.kg.add_edge("a", "b", "contradicts", context="사람이 단 반론")
        self._sync(a, b)
        self.assertIn(("a", "b", "contradicts"), self._edges())

    def test_filename_link_with_block_ref_resolves_to_frontmatter_id(self):
        prev = _write(self.vault / "daily" / "news" / "2026-09-29.md",
                      "id: daily-news-2026-09-29\ntitle: 9월 29일 브리핑")
        cur = _write(self.vault / "daily" / "news" / "2026-09-30.md",
                     "id: daily-news-2026-09-30\ntitle: 9월 30일 브리핑",
                     body="[[2026-09-29#^event-iran|9월 29일 사건 노드]]\n")
        self._sync(prev, cur)
        self.assertIn(("daily-news-2026-09-30", "daily-news-2026-09-29", "links"), self._edges())

    def test_vault_rooted_path_link_resolves(self):
        day = _write(self.vault / "daily" / "news" / "2026-09-24.md", "id: daily-news-2026-09-24\ntitle: 9/24")
        wk = _write(self.vault / "daily" / "news" / "weekly" / "2026-W39.md",
                    "id: weekly-news-2026-W39\ntitle: W39",
                    body="[[docs/daily/news/2026-09-24#^event-iran|09/24]]\n")
        self._sync(day, wk)
        self.assertIn(("weekly-news-2026-W39", "daily-news-2026-09-24", "links"), self._edges())

    def test_bare_filename_prefers_same_folder_then_shortest_path(self):
        news = _write(self.vault / "daily" / "news" / "2026-09-24.md", "id: daily-news-2026-09-24\ntitle: 뉴스 9/24")
        journal = _write(self.vault / "daily" / "2026-09-24.md", "id: journal-2026-09-24\ntitle: 일지 9/24")
        sibling = _write(self.vault / "daily" / "news" / "2026-09-25.md",
                         "id: daily-news-2026-09-25\ntitle: 뉴스 9/25", body="[[2026-09-24]]\n")
        weekly = _write(self.vault / "daily" / "news" / "weekly" / "2026-W39.md",
                        "id: weekly-news-2026-W39\ntitle: W39", body="[[2026-09-24]]\n")
        self._sync(news, journal, sibling, weekly)
        edges = self._edges()
        self.assertIn(("daily-news-2026-09-25", "daily-news-2026-09-24", "links"), edges)
        # weekly/ 에는 동명 파일이 없으니 Obsidian 처럼 가장 짧은 경로로 간다.
        self.assertIn(("weekly-news-2026-W39", "journal-2026-09-24", "links"), edges)


if __name__ == "__main__":
    unittest.main()
