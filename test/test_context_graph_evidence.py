import unittest
from unittest.mock import AsyncMock, patch

from core.context import context_builder


class _SemanticGraph:
    enabled = True

    def __init__(self, graph_hits):
        self.graph_hits = graph_hits

    async def graph_retrieve_from_episodes(self, episode_hits):
        return list(self.graph_hits)


class ContextGraphEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_evidence_renders_bounded_path_and_summary(self):
        graph_hits = [
            {
                "id": "related",
                "score": 0.432,
                "summary": "Graph-aware retrieval decision",
                "path": [
                    {"kind": "episode", "id": "77"},
                    {"kind": "edge", "type": "EP_TO_KG", "rel_type": "semantic"},
                    {"kind": "kg", "id": "anchor", "title": "Memory Roadmap"},
                    {
                        "kind": "edge",
                        "type": "KG_EDGE",
                        "rel_type": "supports",
                        "direction": "out",
                    },
                    {"kind": "kg", "id": "related", "title": "Retrieval Design"},
                ],
            }
        ]
        with patch.object(
            context_builder,
            "get_semantic_graph",
            return_value=_SemanticGraph(graph_hits),
        ):
            snippet = await context_builder._graph_evidence_snippet(
                [{"id": "77", "content": "decision", "score": 0.8}]
            )

        self.assertIn("[0.432] ep#77 -[semantic]-> Memory Roadmap", snippet)
        self.assertIn("-[supports]-> Retrieval Design", snippet)
        self.assertIn("Graph-aware retrieval decision", snippet)

    async def test_build_prompt_injects_graph_evidence_after_filtered_episode(self):
        episode_hits = [{"id": "77", "content": "remembered decision", "score": 0.8}]
        common_patches = (
            patch.object(context_builder, "get_identity", return_value={"name": "Engram", "narrative": ""}),
            patch.object(context_builder, "get_persona", return_value={}),
            patch.object(context_builder, "get_themes", return_value=[]),
            patch.object(context_builder, "render_persona", return_value="persona"),
            patch.object(context_builder, "render_directives_prompt", return_value=""),
            patch.object(context_builder, "render_curiosity_prompt", return_value=""),
            patch.object(context_builder, "_precompute_query_vec", new=AsyncMock(return_value=[1.0])),
            patch.object(context_builder, "_kg_context_snippet", new=AsyncMock(return_value="")),
            patch.object(context_builder, "search_memory_hits", new=AsyncMock(return_value=episode_hits)),
            patch.object(
                context_builder,
                "_graph_evidence_snippet",
                new=AsyncMock(return_value="[0.500] ep#77 -> Retrieval Design"),
            ),
            patch.object(context_builder, "_wiki_reminder_snippet", new=AsyncMock(return_value="")),
        )
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], \
             common_patches[4], common_patches[5], common_patches[6], common_patches[7], \
             common_patches[8], common_patches[9], common_patches[10]:
            prompt = await context_builder.build_system_prompt(
                "graph retrieval decision",
                project_key="ProjectIntelContunuum",
            )

        self.assertIn("<ctx:memories>\nremembered decision\n</ctx:memories>", prompt)
        self.assertIn(
            "<ctx:graph_evidence>\n[0.500] ep#77 -> Retrieval Design\n</ctx:graph_evidence>",
            prompt,
        )

    async def test_no_filtered_episode_means_no_graph_evidence(self):
        graph_mock = AsyncMock(return_value="unexpected")
        with (
            patch.object(context_builder, "get_identity", return_value={"name": "Engram", "narrative": ""}),
            patch.object(context_builder, "get_persona", return_value={}),
            patch.object(context_builder, "get_themes", return_value=[]),
            patch.object(context_builder, "render_persona", return_value="persona"),
            patch.object(context_builder, "render_directives_prompt", return_value=""),
            patch.object(context_builder, "render_curiosity_prompt", return_value=""),
            patch.object(context_builder, "_precompute_query_vec", new=AsyncMock(return_value=[1.0])),
            patch.object(context_builder, "_kg_context_snippet", new=AsyncMock(return_value="")),
            patch.object(context_builder, "search_memory_hits", new=AsyncMock(return_value=[])),
            patch.object(context_builder, "search_memories", new=AsyncMock(return_value=[])),
            patch.object(context_builder, "_graph_evidence_snippet", new=graph_mock),
            patch.object(context_builder, "_wiki_reminder_snippet", new=AsyncMock(return_value="")),
        ):
            prompt = await context_builder.build_system_prompt(
                "medical emotional intelligence study",
                project_key="ProjectIntelContunuum",
            )

        self.assertNotIn("<ctx:graph_evidence>", prompt)
        graph_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
