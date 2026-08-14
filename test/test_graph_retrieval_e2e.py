import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context import context_builder
from core.graph.semantic.semantic_graph import SemanticGraph
from scripts.kg.evaluate_graph_retrieval import evaluate_case


async def _golden_embedding(text: str, _prefix: str) -> list[float]:
    normalized = text.casefold()
    if "medical" in normalized or "emotional intelligence" in normalized:
        return [0.0, 1.0]
    return [1.0, 0.0]


class GraphRetrievalEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "semantic_graph")
        self.sg = SemanticGraph(db_path=db_path)
        self.assertTrue(self.sg.enabled)
        self._embedding_patcher = patch.object(
            SemanticGraph,
            "_compute_embedding",
            side_effect=_golden_embedding,
        )
        self._embedding_patcher.start()

        await self.sg.cypher_query(
            "CREATE (k:KGNode {id: 'graph-memory-roadmap', title: 'Memory Roadmap', "
            "type: 'project', tags: '', summary: 'P1 graph retrieval plan', "
            "embedding: '', content_hash: '', updated_at: ''})"
        )
        await self.sg.cypher_query(
            "CREATE (k:KGNode {id: 'bounded-retrieval', title: 'Bounded Retrieval', "
            "type: 'concept', tags: '', summary: 'Traversal is capped at two hops', "
            "embedding: '', content_hash: '', updated_at: ''})"
        )
        await self.sg.cypher_query(
            "CREATE (k:KGNode {id: 'score-fusion', title: 'Score Fusion', "
            "type: 'concept', tags: '', summary: 'Second-hop rationale uses edge decay', "
            "embedding: '', content_hash: '', updated_at: ''})"
        )
        await self.sg.create_edge(
            "graph-memory-roadmap",
            "bounded-retrieval",
            "contains",
            0.9,
        )
        await self.sg.create_edge(
            "bounded-retrieval",
            "score-fusion",
            "supports",
            0.8,
        )
        await self.sg.cypher_query(
            "CREATE (e:EpisodeNode {id: '77', "
            "content: 'Graph retrieval is bounded to two hops. Related knowledge uses score fusion.', "
            "keywords: 'graph retrieval score fusion', session_id: 'session', "
            "embedding: '[1.0, 0.0]', "
            f"embedding_model: '{self.sg.embedding_model_name}', "
            "created_at: '2026-08-12T00:00:00+00:00'})"
        )
        await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: '77'}), "
            "(k:KGNode {id: 'graph-memory-roadmap'}) "
            "CREATE (e)-[:EP_TO_KG {rel_type: 'semantic', weight: 0.95, score: 0.95, "
            "keywords: '', method: 'semantic', model: 'golden', version: '1', "
            "created_at: '2026-08-12T00:00:00+00:00'}]->(k)"
        )

    async def asyncTearDown(self):
        self._embedding_patcher.stop()
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def _context_patches(self):
        return (
            patch.object(context_builder, "get_semantic_graph", return_value=self.sg),
            patch("core.graph.semantic.get_semantic_graph", return_value=self.sg),
            patch.object(
                context_builder,
                "get_identity",
                return_value={"name": "Engram", "narrative": ""},
            ),
            patch.object(context_builder, "get_persona", return_value={}),
            patch.object(context_builder, "get_themes", return_value=[]),
            patch.object(context_builder, "render_persona", return_value="persona"),
            patch.object(context_builder, "render_directives_prompt", return_value=""),
            patch.object(context_builder, "render_curiosity_prompt", return_value=""),
        )

    async def _build_prompt(self, query: str) -> str:
        patches = self._context_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patches[6], patches[7]:
            return await context_builder.build_system_prompt(query)

    async def test_direct_and_paraphrase_queries_inject_second_hop_evidence(self):
        queries = (
            "Why is graph retrieval bounded to two hops?",
            "How far may an episode expand through related knowledge?",
        )
        for query in queries:
            with self.subTest(query=query):
                prompt = await self._build_prompt(query)
                self.assertIn("<ctx:memories>", prompt)
                self.assertIn("<ctx:graph_evidence>", prompt)
                self.assertIn("Score Fusion", prompt)
                self.assertIn("Second-hop rationale uses edge decay", prompt)

    async def test_unrelated_medical_query_is_a_negative_control(self):
        prompt = await self._build_prompt(
            "medical emotional intelligence clinical study"
        )

        self.assertNotIn("<ctx:memories>", prompt)
        self.assertNotIn("<ctx:graph_evidence>", prompt)

    async def test_golden_case_evaluator_reports_expected_graph_node(self):
        result = await evaluate_case(
            self.sg,
            {
                "name": "bounded-retrieval",
                "query": "Why is graph retrieval bounded to two hops?",
                "expected_kg_ids_any": ["score-fusion"],
            },
        )

        self.assertTrue(result["passed"])
        self.assertIn("77", result["episode_ids"])
        self.assertIn("score-fusion", result["graph_ids"])


if __name__ == "__main__":
    unittest.main()
