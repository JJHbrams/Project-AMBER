import unittest
from unittest.mock import patch

from core.identity import reflection_client


class ReflectionClientOptionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_minimal_system_prompt_for_isolated_calls(self):
        captured = {}

        async def fake_query(*, prompt, options, transport):
            captured["options"] = options
            if False:
                yield None

        with patch.object(reflection_client, "_sdk_query", fake_query):
            text, session_id = await reflection_client._call_async(
                "요약해라",
                session_id=None,
                json_schema=None,
            )

        self.assertEqual(text, "")
        self.assertIsNone(session_id)
        self.assertEqual(
            captured["options"].system_prompt,
            reflection_client._MINIMAL_SYSTEM_PROMPT,
        )
        self.assertEqual(
            captured["options"].extra_args,
            {"strict-mcp-config": None, "tools": "", "safe-mode": None},
        )


if __name__ == "__main__":
    unittest.main()
