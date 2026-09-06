"""Privacy boundary tests for the read-only self-reflection evidence API."""

import unittest
from unittest.mock import patch

from core.identity.evidence import get_self_reflection_evidence


class IdentityEvidenceTests(unittest.TestCase):
    def test_returns_only_bounded_allowlisted_metadata(self):
        node = {
            "id": "engram-연속체-정체성-시스템",
            "title": "정체성 시스템",
            "type": "concept",
            "tags": ["연속성", "성찰"],
            "summary": "안전한 요약",
            "body": "this must never be read",
            "path": "private/wiki.md",
        }
        with patch("core.identity.service.get_identity", return_value={"name": "엔그램", "narrative": "나는 연속성을 소중히 여긴다."}), \
             patch("core.identity.service.get_themes", return_value=[("성찰", 2.0)]), \
             patch("core.graph.knowledge.knowledge_graph.get_kg") as get_kg:
            get_kg.return_value.get_node.return_value = node
            result = get_self_reflection_evidence()

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "엔그램")
        self.assertEqual(result.themes, ("성찰",))
        self.assertEqual(result.wiki_summary, "안전한 요약")
        self.assertFalse(hasattr(result, "body"))
        self.assertFalse(hasattr(result, "path"))

    def test_fails_closed_for_non_allowlisted_node_or_bad_tags(self):
        with patch("core.identity.service.get_identity", return_value={"narrative": "x"}), \
             patch("core.identity.service.get_themes", return_value=[]), \
             patch("core.graph.knowledge.knowledge_graph.get_kg") as get_kg:
            get_kg.return_value.get_node.return_value = {"id": "another-node", "tags": []}
            self.assertIsNone(get_self_reflection_evidence())
            get_kg.return_value.get_node.return_value = {
                "id": "engram-연속체-정체성-시스템", "tags": "not-a-list"
            }
            self.assertIsNone(get_self_reflection_evidence())

    def test_no_identity_content_returns_none(self):
        with patch("core.identity.service.get_identity", return_value={}), \
             patch("core.identity.service.get_themes", return_value=[]), \
             patch("core.graph.knowledge.knowledge_graph.get_kg") as get_kg:
            get_kg.return_value.get_node.return_value = {}
            self.assertIsNone(get_self_reflection_evidence())


if __name__ == "__main__":
    unittest.main()
