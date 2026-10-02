"""증분 kg_sync 에서 대상보다 먼저 sync 된 링크가 나중에 엣지가 되는지.

원자 노트를 한 파일씩 쓰는 동안 sync 가 끼어들면, obs 가 event 보다 먼저 들어가
`links: [event-x]` 가 풀리지 않는다. 예전에는 바뀐 파일만 재해석해서 event 가 생긴
다음 sync 에서도 obs 의 엣지가 영영 생기지 않았다(소급 변환에서 엣지 1,078개 중 23개만 생김).
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server
from core.graph.knowledge.knowledge_graph import KnowledgeGraph


class LateTargetSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.docs = self.vault / "docs"
        self.docs.mkdir()
        db = str(self.vault / "kg.db")

        def gc():
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return c

        self._patches = [
            patch("core.graph.knowledge.knowledge_graph.get_connection", side_effect=gc),
            patch.object(mcp_server, "_vault", return_value=self.vault),
        ]
        for p in self._patches:
            p.start()
        self.kg = KnowledgeGraph()
        self._patches.append(patch.object(mcp_server, "get_kg", return_value=self.kg))
        self._patches[-1].start()
        sg_patch = patch("core.graph.semantic.run_sg_coro", return_value={"status": "test"})
        self._patches.append(sg_patch)
        sg_patch.start()
        sg_stub = type("SG", (), {"sync_from_kg": lambda self: None})()
        self._patches.append(patch.object(mcp_server, "get_semantic_graph", return_value=sg_stub))
        self._patches[-1].start()
        self._db = db

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _write(self, rel, fm):
        p = self.docs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{fm}\n---\n\n본문\n", encoding="utf-8")
        t = time.time() + 1
        os.utime(p, (t, t))

    def _edge(self, a, b):
        c = sqlite3.connect(self._db)
        r = c.execute("SELECT 1 FROM kg_edges WHERE from_id=? AND to_id=?", (a, b)).fetchone()
        c.close()
        return r is not None

    def test_link_resolves_once_target_appears_in_later_sync(self):
        self._write("ontology/obs/obs-a.md", "id: obs-a\ntitle: 관측 A\nlinks:\n  - event-a")
        mcp_server._kg_sync_impl()
        self.assertFalse(self._edge("obs-a", "event-a"))

        self._write("ontology/events/event-a.md", "id: event-a\ntitle: 사건 A")
        mcp_server._kg_sync_impl()
        self.assertTrue(self._edge("obs-a", "event-a"))


if __name__ == "__main__":
    unittest.main()
