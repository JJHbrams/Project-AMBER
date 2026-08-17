import tempfile
import types
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from core.graph.semantic.semantic_graph import SemanticGraph
from core.install.model_manifest import create_manifest, ensure_model, read_manifest


async def _embedding(_text: str, _prefix: str) -> list[float]:
    return [1.0, 0.0]


class EmbeddingModelMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sg = SemanticGraph(
            db_path=str(Path(self._tmpdir.name) / "semantic_graph"),
            embedding_model="intfloat/multilingual-e5-small",
        )
        self.assertTrue(self.sg.enabled)
        self._patcher = patch.object(
            SemanticGraph,
            "_compute_embedding",
            side_effect=_embedding,
        )
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        try:
            self.sg.async_conn.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    async def test_query_and_passage_roles_use_distinct_prefixes(self):
        self._patcher.stop()

        class _Vector:
            def tolist(self):
                return [1.0, 0.0]

        class _Encoder:
            def __init__(self):
                self.inputs = []

            def encode(self, text, **_kwargs):
                self.inputs.append(text)
                return _Vector()

        encoder = _Encoder()
        self.sg._encoder = encoder
        await self.sg.compute_query_embedding("find memory")
        await self.sg.compute_passage_embedding("stored memory")

        self.assertEqual(
            encoder.inputs,
            ["query: find memory", "passage: stored memory"],
        )
        self._patcher.start()

    async def test_model_stamp_mismatch_forces_node_reembedding(self):
        self.assertTrue(
            await self.sg.upsert_node("node", "Title", "concept", [], "Summary")
        )
        await self.sg.cypher_query(
            "MATCH (n:KGNode {id: 'node'}) "
            "SET n.embedding_model = 'legacy-model'"
        )

        self.assertTrue(
            await self.sg.upsert_node("node", "Title", "concept", [], "Summary")
        )
        stale = await self.sg.embedding_staleness()
        self.assertEqual(stale["kg_nodes"], 0)

    async def test_stale_episode_is_excluded_from_search_cache(self):
        await self.sg.cypher_query(
            "CREATE (e:EpisodeNode {id: 'legacy', content: 'legacy memory', "
            "keywords: '', session_id: '', embedding: '[1.0, 0.0]', "
            "embedding_model: 'legacy-model', created_at: ''})"
        )

        results = await self.sg.episode_semantic_search(
            "legacy memory",
            threshold=0.0,
        )
        stale = await self.sg.embedding_staleness()

        self.assertEqual(results, [])
        self.assertEqual(stale["episodes"], 1)

    def test_manifest_pinned_export_is_replaced_instead_of_relabelled(self):
        model_dir = Path(self._tmpdir.name) / "legacy-model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "model.safetensors").write_bytes(b"e5")
        manifest = create_manifest(
            model_dir,
            model_id="intfloat/multilingual-e5-small",
            resolved_revision="test-revision",
        )
        (model_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (model_dir / "legacy.bin").write_bytes(b"legacy")

        cache = Path(self._tmpdir.name) / "hub-cache"
        cache.mkdir()
        (cache / "config.json").write_text("{}", encoding="utf-8")
        (cache / "model.safetensors").write_bytes(b"e5")

        def fake_download(**kwargs):
            return cache / kwargs["filename"]

        fake_module = types.SimpleNamespace(hf_hub_download=fake_download)
        with patch.dict("sys.modules", {"huggingface_hub": fake_module}):
            result = ensure_model(
                model_dir,
                model_id="intfloat/multilingual-e5-small",
                allow_download=True,
            )

        self.assertEqual(result, "exported")
        self.assertFalse((model_dir / "legacy.bin").exists())
        self.assertEqual(
            read_manifest(model_dir)["model_id"],
            "intfloat/multilingual-e5-small",
        )
