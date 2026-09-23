"""The MCP persona context exposes a bounded outcome, never evaluator internals."""

import asyncio
import unittest
from unittest.mock import patch

from core.context.context_builder import build_system_prompt
from core.identity.service import render_persona


class ContextPersonalityModuleTests(unittest.TestCase):
    def test_raw_humor_scalar_is_not_rendered_by_persona(self):
        rendered = render_persona({"humor": .77})
        self.assertNotIn("humor:", rendered)
        self.assertIn("warmth:", rendered)

    def test_context_has_one_short_situational_signal_without_classifier_terms(self):
        with patch("core.context.context_builder.get_identity", return_value={}), patch(
            "core.context.context_builder.get_persona", return_value={"humor": .8}
        ), patch("core.context.context_builder.get_themes", return_value=[]), patch(
            "core.context.context_builder.render_persona", return_value="voice: dry"
        ):
            context = asyncio.run(build_system_prompt("문서를 정리해줘", session_key=41))
        signals = [line for line in context.splitlines() if line.startswith("situational_humor:")]
        self.assertEqual(len(signals), 1)
        self.assertLessEqual(len(signals[0]), 120)
        self.assertNotIn("_SENSITIVE_TERMS", context)
        self.assertNotIn("keyword", context.casefold())

    def test_evaluator_failure_keeps_context_available(self):
        with patch("core.context.context_builder.evaluate_situational_humor", side_effect=RuntimeError), patch(
            "core.context.context_builder.get_identity", return_value={}
        ), patch("core.context.context_builder.get_persona", return_value={}), patch(
            "core.context.context_builder.get_themes", return_value=[]
        ):
            context = asyncio.run(build_system_prompt("ordinary request"))
        self.assertIn("[persona]", context)
        self.assertNotIn("situational_humor:", context)


if __name__ == "__main__":
    unittest.main()
