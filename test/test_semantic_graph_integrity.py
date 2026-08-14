import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.graph.semantic.semantic_graph import SemanticGraph


async def _no_embedding(text: str, _prefix: str) -> list[float]:
    return []


class SemanticGraphIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sg = SemanticGraph(
            db_path=str(Path(self._tmpdir.name) / "semantic_graph")
        )
        self.assertTrue(self.sg.enabled, "kuzu가 설치되어 있어야 함(테스트 환경 전제)")
        self._patcher = patch.object(
            SemanticGraph,
            "_compute_embedding",
            side_effect=_no_embedding,
        )
        self._patcher.start()

        await self.sg.upsert_node("kg-1", "KG", "concept", [], "node")
        for episode_id in ("10", "2", "alpha", "zeta", "keep"):
            self.assertTrue(
                await self.sg.upsert_episode(episode_id, f"content {episode_id}")
            )
        await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: $eid}), (k:KGNode {id: $kid}) "
            "MERGE (e)-[:EP_TO_KG {rel_type: 'test'}]->(k)",
            {"eid": "10", "kid": "kg-1"},
        )
        await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: $eid}), (k:KGNode {id: $kid}) "
            "MERGE (e)-[:EP_TO_KG {rel_type: 'test'}]->(k)",
            {"eid": "keep", "kid": "kg-1"},
        )
        self.canonical_ids = ["keep", "beta", "3"]

    async def asyncTearDown(self):
        self._patcher.stop()
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    async def test_dry_run_reports_integrity_without_mutation(self):
        report = await self.sg.reconcile_episodes(self.canonical_ids)

        self.assertEqual(report["canonical_count"], 3)
        self.assertEqual(report["episode_count_before"], 5)
        self.assertEqual(report["stale_ids"], ["2", "10", "alpha", "zeta"])
        self.assertEqual(report["missing_ids"], ["3", "beta"])
        self.assertEqual(report["unlinked_episode_ids"], ["2", "alpha", "zeta"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["deleted_count"], 0)
        self.assertEqual(report["episode_count_after"], 5)
        self.assertEqual(
            await self.sg.count("MATCH (e:EpisodeNode) RETURN count(e)"),
            5,
        )

    async def test_canonical_snapshot_is_loaded_under_graph_lock(self):
        def load_canonical_ids():
            self.assertTrue(self.sg._write_lock.locked())
            return self.canonical_ids

        report = await self.sg.reconcile_episodes(load_canonical_ids)

        self.assertEqual(report["canonical_count"], 3)

    async def test_apply_deletes_stale_nodes_and_relationships(self):
        self.sg._episode_cache_dirty = False
        report = await self.sg.reconcile_episodes(self.canonical_ids, apply=True)

        self.assertTrue(report["applied"])
        self.assertEqual(report["deleted_count"], 4)
        self.assertEqual(report["episode_count_after"], 1)
        self.assertTrue(self.sg._episode_cache_dirty)
        self.assertEqual(
            await self.sg.count("MATCH ()-[r:EP_TO_KG]->() RETURN count(r)"),
            1,
        )

    async def test_second_apply_is_idempotent(self):
        await self.sg.reconcile_episodes(self.canonical_ids, apply=True)
        report = await self.sg.reconcile_episodes(self.canonical_ids, apply=True)

        self.assertEqual(report["stale_ids"], [])
        self.assertEqual(report["missing_ids"], ["3", "beta"])
        self.assertEqual(report["deleted_count"], 0)
        self.assertEqual(report["episode_count_before"], 1)
        self.assertEqual(report["episode_count_after"], 1)


if __name__ == "__main__":
    unittest.main()
