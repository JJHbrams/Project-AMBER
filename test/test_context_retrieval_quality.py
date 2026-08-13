import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.memory import store
from core.context import project_scope


class _SemanticGraph:
    enabled = True

    def __init__(self, hits):
        self.hits = hits

    async def episode_semantic_search(self, *args, **kwargs):
        return list(self.hits)


class ContextRetrievalQualityTests(unittest.IsolatedAsyncioTestCase):
    def test_portable_kg_mapping_ignores_project_path_digest(self):
        with patch.object(
            project_scope,
            "get_cfg_value",
            return_value={"projectintelcontunuum": "graph-memory-roadmap"},
        ):
            node_id = project_scope.resolve_kg_node_id("projectintelcontunuum-deadbeef")

        self.assertEqual(node_id, "graph-memory-roadmap")

    async def test_project_match_reranks_paraphrase_above_cross_project_hit(self):
        hits = [
            {
                "id": "other",
                "content": "---\nproject: OtherProject\n---\n\n비슷하지만 다른 프로젝트의 결정",
                "score": 0.50,
                "created_at": "",
            },
            {
                "id": "target",
                "content": "---\nproject: CurrentProject\n---\n\n현재 프로젝트에서 정한 실제 결정",
                "score": 0.48,
                "created_at": "",
            },
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits(
                "현재 결정을 바꿔 말해 회상",
                limit=1,
                project_key="CurrentProject",
            )

        self.assertEqual([hit["id"] for hit in result], ["target"])
        self.assertGreater(result[0]["score"], result[0]["raw_score"])

    async def test_short_test_episode_is_excluded(self):
        hits = [
            {"id": "test", "content": "비동기 upsert 테스트", "score": 0.91, "created_at": ""},
            {
                "id": "real",
                "content": "---\nproject: CurrentProject\n---\n\n실제 세션에서 결정한 회상 기준",
                "score": 0.50,
                "created_at": "",
            },
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits("회상 기준", limit=2, project_key="CurrentProject")

        self.assertEqual([hit["id"] for hit in result], ["real"])

    def test_test_marker_does_not_match_inside_regular_english_word(self):
        self.assertFalse(store._is_test_episode("Latest release decision"))
        self.assertFalse(store._is_test_episode("Contest winner selected"))

    async def test_low_relevance_cross_project_hits_are_suppressed(self):
        hits = [
            {
                "id": "noise",
                "content": "---\nproject: OtherProject\n---\n\n무관한 프로젝트 기억",
                "score": 0.42,
                "created_at": "",
            }
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits("현재 프로젝트 질문", limit=2, project_key="CurrentProject")

        self.assertEqual(result, [])

    async def test_hangul_hit_requires_query_token_evidence(self):
        hits = [
            {
                "id": "semantic-noise",
                "content": (
                    "---\nproject: CurrentProject\n---\n\n"
                    "페르소나 지침의 적용 범위와 \"충돌 아님\"을 정리했다."
                ),
                "score": 0.80,
                "created_at": "",
            }
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits(
                "의료 환경에서 감성지능(EI)을 측정하는 임상 연구",
                limit=2,
                project_key="CurrentProject",
            )

        self.assertEqual(result, [])

    async def test_hangul_paraphrase_keeps_normalized_token_evidence(self):
        hits = [
            {
                "id": "target",
                "content": (
                    "---\nproject: CurrentProject\n---\n\n"
                    "negative control의 무관 기억 허용치는 0건이다. "
                    "직접 질문과 패러프레이즈 모두 같은 결정을 회상해야 한다."
                ),
                "score": 0.49,
                "created_at": "",
            }
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits(
                "기억 검색 질문을 바꿔 말해도 무관한 분야가 섞이지 않는 기준",
                limit=2,
                project_key="CurrentProject",
            )

        self.assertEqual([hit["id"] for hit in result], ["target"])

    async def test_project_boost_does_not_rescue_low_raw_similarity(self):
        hits = [
            {
                "id": "noise",
                "content": "---\nproject: CurrentProject\n---\n\n같은 프로젝트지만 무관한 기억",
                "score": 0.46,
                "created_at": "",
            }
        ]
        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            result = await store.search_memory_hits("현재 프로젝트 질문", limit=2, project_key="CurrentProject")

        self.assertEqual(result, [])

    async def test_declared_negative_control_subject_is_excluded(self):
        content = (
            "---\nproject: CurrentProject\n---\n\n"
            "직접 질문과 패러프레이즈에서 같은 결정을 회상한다.\n\n"
            "다음 작업: 의료 EI 무관 질의에서 회상 품질을 재측정한다."
        )
        hits = [{"id": "canary", "content": content, "score": 0.80, "created_at": ""}]

        with patch("core.graph.semantic.get_semantic_graph", return_value=_SemanticGraph(hits)):
            negative = await store.search_memory_hits(
                "의료 환경의 감성지능 EI 연구",
                limit=2,
                project_key="CurrentProject",
            )
            positive = await store.search_memory_hits(
                "직접 질문과 패러프레이즈 회상 기준",
                limit=2,
                project_key="CurrentProject",
            )

        self.assertEqual(negative, [])
        self.assertEqual([hit["id"] for hit in positive], ["canary"])

    async def test_sqlite_fallback_applies_project_ranking_and_test_filter(self):
        class _Rows:
            def fetchall(self):
                return [
                    {
                        "content": "비동기 upsert 테스트",
                        "keywords": "현재 프로젝트 질문",
                    },
                    {
                        "content": "---\nproject: Other Project\n---\n\n다른 프로젝트 기억",
                        "keywords": "현재 프로젝트 질문",
                    },
                    {
                        "content": "---\nproject: Current Project\n---\n\n현재 프로젝트 기억",
                        "keywords": "현재 프로젝트 질문",
                    },
                ]

        class _Connection:
            def execute(self, *args, **kwargs):
                return _Rows()

            def close(self):
                pass

        with (
            patch("core.graph.semantic.get_semantic_graph", side_effect=RuntimeError("semantic unavailable")),
            patch.object(store, "get_connection", return_value=_Connection()),
        ):
            result = await store.search_memories(
                "현재 프로젝트 질문",
                limit=2,
                project_key="Current Project",
            )

        self.assertEqual(result[0], "---\nproject: Current Project\n---\n\n현재 프로젝트 기억")
        self.assertNotIn("비동기 upsert 테스트", result)


if __name__ == "__main__":
    unittest.main()
