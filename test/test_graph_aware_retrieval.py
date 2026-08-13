import tempfile
import unittest
from pathlib import Path

from core.graph.semantic.semantic_graph import SemanticGraph


class GraphAwareRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "semantic_graph")
        self.sg = SemanticGraph(db_path=db_path)
        self.assertTrue(self.sg.enabled)

        await self.sg.upsert_node("anchor", "Anchor", "project", [], "Direct anchor")
        await self.sg.upsert_node("hop-1", "Hop One", "concept", [], "First related node")
        await self.sg.upsert_node("hop-2", "Hop Two", "concept", [], "Second related node")
        await self.sg.upsert_node("hop-3", "Hop Three", "concept", [], "Out of bounds")
        await self.sg.create_edge("anchor", "hop-1", "contains", 0.8)
        await self.sg.create_edge("hop-1", "hop-2", "supports", 0.5)
        await self.sg.create_edge("hop-2", "hop-3", "extends", 1.0)
        await self.sg.cypher_query(
            "CREATE (e:EpisodeNode {id: 'ep', content: 'episode', keywords: '', "
            "session_id: '', embedding: '', created_at: 'now'})"
        )
        await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: 'ep'}), (k:KGNode {id: 'anchor'}) "
            "CREATE (e)-[:EP_TO_KG {rel_type: 'semantic', weight: 0.9, score: 0.9, "
            "keywords: '', method: 'semantic', model: 'test', version: '1', "
            "created_at: 'now'}]->(k)"
        )

    async def asyncTearDown(self):
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    async def test_retrieval_is_bounded_to_two_hops_and_fuses_scores(self):
        results = await self.sg.graph_retrieve_from_episodes(
            [{"id": "ep", "score": 0.8}],
            max_hops=99,
            top_k=0,
            hop_decay=0.75,
            min_score=0.0,
        )
        by_id = {item["id"]: item for item in results}

        self.assertEqual(set(by_id), {"anchor", "hop-1", "hop-2"})
        self.assertAlmostEqual(by_id["anchor"]["score"], 0.72)
        self.assertAlmostEqual(by_id["hop-1"]["score"], 0.432)
        self.assertAlmostEqual(by_id["hop-2"]["score"], 0.162)
        self.assertEqual(by_id["hop-2"]["hop"], 2)
        self.assertEqual(
            [step["id"] for step in by_id["hop-2"]["path"] if step["kind"] != "edge"],
            ["ep", "anchor", "hop-1", "hop-2"],
        )

    async def test_incoming_edges_are_traversed_and_top_k_is_applied(self):
        await self.sg.upsert_node("incoming", "Incoming", "concept", [], "Incoming relation")
        await self.sg.create_edge("incoming", "anchor", "references", 1.0)

        results = await self.sg.graph_retrieve_from_episodes(
            [{"id": "ep", "score": 0.8}],
            max_hops=1,
            top_k=2,
            hop_decay=1.0,
            min_score=0.0,
        )

        self.assertEqual([item["id"] for item in results], ["anchor", "incoming"])
        incoming_edge = next(
            step for step in results[1]["path"] if step.get("type") == "KG_EDGE"
        )
        self.assertEqual(incoming_edge["direction"], "in")

    async def test_missing_episode_links_return_no_results(self):
        results = await self.sg.graph_retrieve_from_episodes(
            [{"id": "missing", "score": 1.0}],
            min_score=0.0,
        )

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
