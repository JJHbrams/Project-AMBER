"""SemanticGraph의 kuzu.AsyncConnection 마이그레이션 동시성 회귀 테스트.

이 레이어(core/graph/semantic)에는 기존 테스트가 전혀 없었다. 여기서는 실제
온디스크 KuzuDB 임시 인스턴스(싱글턴 get_semantic_graph()는 우회)를 써서,
- 동시 upsert/search가 예외 없이 끝나는지
- 캐시 스냅샷이 인덱스 밀림 없이 정합적인지(구 TOCTOU 버그가 있었다면 여기서 잡힘)
- RLock→Lock 전환 중 재도입될 수 있는 데드락이 없는지(wait_for 타임아웃으로 감시)
- run_sg_coro 헬퍼가 이벤트 루프 없는 스레드에서 실제로 동작하는지
를 검증한다. 임베딩은 실제 모델/네트워크 의존을 없애기 위해 결정론적 벡터로 patch한다.
"""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph.semantic.semantic_graph import SemanticGraph, run_sg_coro


def _fake_embedding(text: str) -> list[float]:
    """텍스트 → 결정론적 정규화 벡터. 실제 sentence-transformers 대신 사용."""
    import hashlib

    import numpy as np

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec = np.frombuffer(digest[:32], dtype=np.uint8).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm == 0:
        return [0.0] * len(vec)
    return (vec / norm).tolist()


async def _fake_compute_embedding(text: str) -> list[float]:
    # sleep(0)으로 실제 이벤트 루프 양보 지점을 보장 — 동시성 테스트가 우연히
    # 순차 실행으로 안 흐르도록(실제 AsyncConnection.execute도 스레드풀 경유라
    # 이 자체로도 양보하지만, 임베딩 단계에서도 명시적으로 보장해둔다).
    await asyncio.sleep(0)
    return _fake_embedding(text)


class SemanticGraphConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "semantic_graph")
        self.sg = SemanticGraph(db_path=db_path)
        self.assertTrue(self.sg.enabled, "kuzu가 설치되어 있어야 함(테스트 환경 전제)")

        self._patcher = patch.object(
            SemanticGraph, "compute_embedding", side_effect=_fake_compute_embedding
        )
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    # ── 1. 베이스라인 ────────────────────────────────────────────────────────

    async def test_upsert_node_basic(self):
        await self.sg.upsert_node("n1", "Title One", "concept", ["tag1"], "Summary one")
        count = await self.sg.count("MATCH (n:KGNode) RETURN count(n)")
        self.assertEqual(count, 1)

        results = await self.sg.semantic_search("anything", top_k=5, threshold=-1.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "n1")

    # ── 2. 동시 upsert ───────────────────────────────────────────────────────

    async def test_concurrent_upserts_no_exception(self):
        tasks = [
            self.sg.upsert_node(f"n{i}", f"Title {i}", "concept", [], f"Summary {i}")
            for i in range(20)
        ]
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 20)

        count = await self.sg.count("MATCH (n:KGNode) RETURN count(n)")
        self.assertEqual(count, 20)

    # ── 3. upsert 도중 search — 구 TOCTOU 레이스 회귀 테스트 ───────────────────

    async def test_concurrent_search_during_upsert(self):
        for i in range(5):
            await self.sg.upsert_node(f"seed{i}", f"Seed {i}", "concept", [], f"Seed summary {i}")

        async def _upsert(i):
            await self.sg.upsert_node(f"new{i}", f"New {i}", "concept", [], f"New summary {i}")

        async def _search(i):
            results = await self.sg.semantic_search(f"query{i}", top_k=20, threshold=-1.0)
            for r in results:
                # 인덱스가 밀렸다면 이 키들 중 하나가 없거나 타입이 깨진다.
                self.assertIn("id", r)
                self.assertIn("title", r)
                self.assertIn("summary", r)
                self.assertIsInstance(r["score"], float)
            return results

        tasks = [_upsert(i) for i in range(10)] + [_search(i) for i in range(10)]
        await asyncio.gather(*tasks)

        count = await self.sg.count("MATCH (n:KGNode) RETURN count(n)")
        self.assertEqual(count, 15)  # seed 5 + new 10

    # ── 4. 캐시 스냅샷 정합성 ────────────────────────────────────────────────

    async def test_semantic_search_matches_cache_snapshot(self):
        for i in range(8):
            await self.sg.upsert_node(f"n{i}", f"Title{i}", "concept", [], f"Summary{i}")

        results = await self.sg.semantic_search("query", top_k=8, threshold=-1.0)
        self.assertEqual(len(results), 8)

        seen_ids = set()
        for r in results:
            idx = r["id"].removeprefix("n")
            self.assertEqual(r["title"], f"Title{idx}")
            self.assertEqual(r["summary"], f"Summary{idx}")
            seen_ids.add(r["id"])
        self.assertEqual(len(seen_ids), 8)

    # ── 5/6. RLock→Lock 전환 데드락 회귀(타임아웃 가드) ─────────────────────

    async def test_link_episode_to_kg_no_deadlock(self):
        await self.sg.upsert_node("kgn1", "Related Node", "concept", [], "Related summary")
        ok = await asyncio.wait_for(
            self.sg.upsert_episode("ep1", "Some episode content", keywords="", session_id="s1"),
            timeout=5,
        )
        self.assertTrue(ok)
        count = await self.sg.count("MATCH (e:EpisodeNode) RETURN count(e)")
        self.assertEqual(count, 1)

    async def test_sync_all_ep_to_kg_no_deadlock(self):
        for i in range(3):
            await self.sg.upsert_node(f"kg{i}", f"KG {i}", "concept", [], f"KG summary {i}")
        for i in range(3):
            await self.sg.upsert_episode(f"ep{i}", f"Episode content {i}", keywords="", session_id="s1")

        result = await asyncio.wait_for(self.sg.sync_all_ep_to_kg(), timeout=10)
        self.assertEqual(result["processed"], 3)

    # ── 7. cypher_query 쓰기 후 캐시 무효화 ──────────────────────────────────

    async def test_cypher_query_write_invalidates_cache(self):
        await self.sg.upsert_node("n1", "Old Title", "concept", [], "Old summary")
        # 캐시 워밍(여기서 스테일 캐시가 안 지워지면 아래 검증이 구 값을 봄)
        await self.sg.semantic_search("anything", top_k=5, threshold=-1.0)

        await self.sg.cypher_query(
            "MATCH (n:KGNode {id: $id}) SET n.title = $title",
            {"id": "n1", "title": "New Title"},
        )

        results = await self.sg.semantic_search("anything", top_k=5, threshold=-1.0)
        titles = {r["id"]: r["title"] for r in results}
        self.assertEqual(titles["n1"], "New Title")

    # ── 8. sync_from_kg 동시 실행 가드 ───────────────────────────────────────

    async def test_sync_from_kg_concurrent_guard(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE kg_nodes (id TEXT, title TEXT, type TEXT, tags TEXT, summary TEXT)")
        conn.execute("CREATE TABLE kg_edges (from_id TEXT, to_id TEXT, rel_type TEXT, weight REAL)")
        conn.execute("INSERT INTO kg_nodes VALUES ('a', 'A', 'concept', '[]', 'summary a')")
        conn.commit()

        with patch("core.storage.db.get_connection", return_value=conn):
            results = await asyncio.gather(
                self.sg.sync_from_kg(),
                self.sg.sync_from_kg(),
            )
        statuses = sorted(r["status"] for r in results)
        self.assertEqual(statuses, ["ok", "skipped"])

    # ── 9. 이벤트 루프 없는 스레드에서 run_sg_coro ──────────────────────────

    async def test_run_sg_coro_from_thread(self):
        def _worker():
            run_sg_coro(
                self.sg.upsert_node("threaded1", "Threaded Title", "concept", [], "Threaded summary")
            )

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

        count = await self.sg.count("MATCH (n:KGNode {id: 'threaded1'}) RETURN count(n)")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
