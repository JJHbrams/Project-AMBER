import asyncio
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kuzu

from core.graph.semantic.semantic_graph import (
    EP_TO_KG_LINK_VERSION,
    SemanticGraph,
    _kg_candidate_allowed,
)


async def _embedding(text: str, _prefix: str) -> list[float]:
    if "unrelated" in text.casefold():
        return [0.0, 1.0]
    return [1.0, 0.0]


class SemanticEpToKgMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "semantic_graph")
        self.sg = SemanticGraph(
            db_path=self.db_path,
            embedding_model="intfloat/multilingual-e5-small",
        )
        self.assertTrue(self.sg.enabled)
        self._patcher = patch.object(SemanticGraph, "_compute_embedding", side_effect=_embedding)
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    async def _edges(self, episode_id: str) -> list[dict]:
        return await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: $id})-[r:EP_TO_KG]->(k:KGNode) "
            "RETURN k.id AS kg_id, r.rel_type AS rel_type, r.weight AS weight, "
            "r.keywords AS keywords, r.score AS score, r.method AS method, "
            "r.model AS model, r.version AS version, r.created_at AS created_at",
            {"id": episode_id},
        )

    async def test_semantic_edge_stores_provenance(self):
        await self.sg.upsert_node("kg", "Related", "concept", [], "same vector")
        self.assertTrue(await self.sg.upsert_episode("ep", "same vector"))

        edge = next(row for row in await self._edges("ep") if row["rel_type"] == "semantic")
        self.assertAlmostEqual(edge["score"], edge["weight"])
        self.assertEqual(edge["method"], "semantic")
        self.assertEqual(edge["model"], self.sg._embedding_model_stamp)
        self.assertEqual(edge["version"], EP_TO_KG_LINK_VERSION)
        self.assertEqual(edge["keywords"], "")
        self.assertTrue(edge["created_at"])

    async def test_json_tags_title_and_deterministic_keyword_metadata(self):
        await self.sg.upsert_node("tagged", "Gamma Guide", "concept", '["Beta", "Alpha"]', "unrelated")
        await self.sg.upsert_node("title-only", "Alpha Beta Reference", "concept", "", "unrelated")
        await self.sg.upsert_episode("ep", "unrelated", keywords='["beta", "alpha"]')

        keyword_edges = {
            row["kg_id"]: row for row in await self._edges("ep") if row["rel_type"] == "keyword"
        }
        self.assertEqual(keyword_edges["tagged"]["keywords"], "alpha, beta")
        self.assertEqual(keyword_edges["tagged"]["weight"], 2.0)
        self.assertGreater(keyword_edges["tagged"]["score"], 0.0)
        self.assertLessEqual(keyword_edges["tagged"]["score"], 1.0)
        self.assertEqual(keyword_edges["tagged"]["method"], "keyword")
        self.assertEqual(keyword_edges["tagged"]["model"], "")
        self.assertIn("title-only", keyword_edges)

    async def test_immediate_upsert_and_batch_relink_recreate_edges(self):
        await self.sg.upsert_node("kg", "Alpha Beta Node", "concept", [], "same vector")
        await self.sg.upsert_episode("ep", "same vector", keywords='["alpha", "beta"]')
        immediate = await self._edges("ep")
        self.assertTrue(any(row["rel_type"] == "semantic" for row in immediate))
        self.assertTrue(any(row["rel_type"] == "keyword" for row in immediate))

        await self.sg.cypher_query(
            "MATCH (e:EpisodeNode {id: 'ep'})-[r:EP_TO_KG]->() "
            "WHERE r.rel_type IN ['semantic', 'keyword'] DELETE r"
        )
        result = await self.sg.sync_all_ep_to_kg(sem_threshold=0.4, top_k=3)
        self.assertEqual(result["processed"], 1)
        self.assertGreaterEqual(result["linked"], 2)
        relinked = await self._edges("ep")
        self.assertTrue(any(row["rel_type"] == "semantic" for row in relinked))
        self.assertTrue(any(row["rel_type"] == "keyword" for row in relinked))

    async def test_batch_relink_includes_keyword_only_episode_without_embedding(self):
        await self.sg.upsert_node("kg", "Alpha Beta Node", "concept", [], "unrelated")
        await self.sg.cypher_query(
            "CREATE (e:EpisodeNode {id: 'keyword-only', content: 'legacy', "
            "keywords: '[\"alpha\", \"beta\"]', session_id: '', embedding: '', created_at: 'now'})"
        )

        result = await self.sg.sync_all_ep_to_kg(sem_threshold=0.4, top_k=3)

        self.assertEqual(result["processed"], 1)
        edges = await self._edges("keyword-only")
        self.assertTrue(any(row["rel_type"] == "keyword" for row in edges))

    async def test_keyword_links_are_ranked_and_capped(self):
        for index in range(6):
            await self.sg.upsert_node(
                f"kg-{index}",
                f"Alpha Beta Topic {index}",
                "concept",
                [],
                "unrelated",
            )
        await self.sg.upsert_episode(
            "ep",
            "unrelated",
            keywords='["alpha", "beta", "gamma"]',
        )

        keyword_edges = [
            row for row in await self._edges("ep") if row["rel_type"] == "keyword"
        ]
        self.assertEqual(len(keyword_edges), 3)
        self.assertEqual(
            sorted(row["kg_id"] for row in keyword_edges),
            ["kg-0", "kg-1", "kg-2"],
        )

    def test_project_scope_and_test_node_gate(self):
        anchor = "graph-memory-roadmap"
        group = "000_project_engram"
        self.assertTrue(
            _kg_candidate_allowed(
                "memory-design",
                "research",
                "projects/000_Project_Engram/dev/memory.md",
                "Memory Design",
                anchor,
                group,
            )
        )
        self.assertTrue(
            _kg_candidate_allowed(
                "global-reference",
                "reference",
                "references/global.md",
                "Global Reference",
                anchor,
                group,
            )
        )
        self.assertFalse(
            _kg_candidate_allowed(
                "truview-project",
                "project",
                "projects/001_TruviewCADMOM/project.md",
                "Truview Project",
                anchor,
                group,
            )
        )
        self.assertFalse(
            _kg_candidate_allowed(
                "subdir-param-verify",
                "project",
                "projects/000_Project_Engram/test.md",
                "subdir-param-verify",
                anchor,
                group,
            )
        )
        self.assertFalse(
            _kg_candidate_allowed(
                "engram-project-node",
                "project",
                "projects/000_Project_Engram/project.md",
                "Engram Project",
                "",
                "__unresolved__",
            )
        )

    async def test_atomic_relink_rolls_back_on_replacement_failure(self):
        await self.sg.upsert_node("old", "Old Link", "concept", [], "same vector")
        await self.sg.upsert_episode("ep", "same vector")
        before = await self._edges("ep")
        self.assertTrue(before)

        with self.assertRaises(ValueError):
            await asyncio.to_thread(
                self.sg._replace_episode_links_transaction,
                "ep",
                [{"id": "old", "score": "not-a-number"}],
                [],
                "now",
            )

        after = await self._edges("ep")
        self.assertEqual(
            sorted((row["kg_id"], row["rel_type"]) for row in after),
            sorted((row["kg_id"], row["rel_type"]) for row in before),
        )

    async def test_candidate_failure_preserves_existing_links(self):
        await self.sg.upsert_node("old", "Old Link", "concept", [], "same vector")
        await self.sg.upsert_episode("ep", "same vector")
        before = await self._edges("ep")
        self.assertTrue(before)

        with patch.object(
            self.sg,
            "_semantic_search_locked",
            side_effect=RuntimeError("candidate search failed"),
        ):
            created = await self.sg.link_episode_to_kg("ep", [1.0, 0.0])

        self.assertEqual(created, 0)
        after = await self._edges("ep")
        self.assertEqual(
            sorted((row["kg_id"], row["rel_type"]) for row in after),
            sorted((row["kg_id"], row["rel_type"]) for row in before),
        )


class SemanticEpToKgMigrationTests(unittest.TestCase):
    def test_migrates_legacy_ep_to_kg_relationship_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "semantic_graph")
            db = kuzu.Database(db_path)
            conn = kuzu.Connection(db)
            conn.execute("CREATE NODE TABLE KGNode(id STRING PRIMARY KEY)")
            conn.execute("CREATE NODE TABLE EpisodeNode(id STRING PRIMARY KEY)")
            conn.execute(
                "CREATE REL TABLE EP_TO_KG(FROM EpisodeNode TO KGNode, rel_type STRING)"
            )
            conn.close()
            del conn
            del db
            gc.collect()

            sg = SemanticGraph(db_path=db_path)
            self.assertTrue(sg.enabled)
            check = sg.async_conn.acquire_connection()
            try:
                check.execute("CREATE (e:EpisodeNode {id: 'ep'})")
                check.execute("CREATE (k:KGNode {id: 'kg'})")
                check.execute(
                    "MATCH (e:EpisodeNode {id: 'ep'}), (k:KGNode {id: 'kg'}) "
                    "CREATE (e)-[r:EP_TO_KG {rel_type: 'semantic'}]->(k) "
                    "SET r.weight=0.5, r.keywords='', r.score=0.5, "
                    "r.method='semantic', r.model='model', r.version='1', "
                    "r.created_at='now'"
                )
                result = check.execute(
                    "MATCH ()-[r:EP_TO_KG]->() RETURN r.weight, r.keywords, r.score, "
                    "r.method, r.model, r.version, r.created_at"
                )
                self.assertEqual(
                    result.get_next(),
                    [0.5, "", 0.5, "semantic", "model", "1", "now"],
                )
            finally:
                sg.async_conn.release_connection(check)
                sg.async_conn.close()


if __name__ == "__main__":
    unittest.main()
