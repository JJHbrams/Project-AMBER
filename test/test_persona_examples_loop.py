"""AC-11 — 루프가 실제로 닫히는지.

저장 수단이 있어도 쌓이는 계기가 없으면 기능이 아니다. `persona_observations` 가
75일 42세션 동안 한 번도 채워지지 않은 전례가 있어서, 여기서는 "도구가 존재한다"가
아니라 "종료 경로가 예시를 적재한다"를 검사한다.

경로가 갈리는 것도 잡는다 — 인자를 엉뚱한 도구에 붙여 두면 스킬이 부르는 쪽에는
아무 일도 일어나지 않는다. 실제로 한 번 그렇게 만들었다.
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.storage.db import initialize_db

_SKILL = Path.home() / ".claude" / "skills" / "engram-close-session" / "SKILL.md"


class PersonaExamplesLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        self._patch = patch("core.storage.db._get_db_dir", return_value=self.db_dir)
        self._patch.start()
        initialize_db(self.db_dir)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _store(self, *args):
        from mcp_server import _store_reflection_example

        return _store_reflection_example(*args)

    def test_reflection_stores_into_an_empty_tag(self):
        from core.identity.examples import list_examples

        saved, skipped = self._store("자극이 된 상황", "그때 한 말", "situation", "정정")
        self.assertEqual(skipped, "")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["origin"], "auto")
        self.assertEqual([r["id"] for r in list_examples()], [saved["id"]])

    def test_full_tag_is_refused_with_a_reason(self):
        from core.identity.examples import TAG_CAP, add_example

        for n in range(TAG_CAP):
            add_example(f"자극 {n}", f"응답 {n}", tag="정정")
        saved, skipped = self._store("또 다른 상황", "또 다른 말", "situation", "정정")
        self.assertIsNone(saved)
        self.assertIn("남은 자리가 없다", skipped)

    def test_overlong_pair_is_refused_with_a_reason(self):
        from core.identity.examples import AUTO_MAX_PAIR_CHARS

        long_text = "".join(chr(0xac00 + (i * 7) % 2000) for i in range(AUTO_MAX_PAIR_CHARS))
        saved, skipped = self._store("자극", long_text, "situation", "정정")
        self.assertIsNone(saved)
        self.assertIn("상한", skipped)

    def test_empty_input_is_a_no_op_not_an_error(self):
        saved, skipped = self._store("", "", "situation", "정정")
        self.assertIsNone(saved)
        self.assertEqual(skipped, "")

    def test_close_session_is_the_tool_that_takes_the_example(self):
        """스킬이 부르는 도구에 인자가 있어야 한다. apply_reflection 에만 있으면 아무 일도 안 난다."""
        import inspect

        import mcp_server

        for name in ("engram_close_session", "engram_apply_reflection"):
            tool = getattr(mcp_server, name)
            fn = getattr(tool, "fn", tool)
            params = inspect.signature(fn).parameters
            for arg in ("example_prompt_text", "example_response_text",
                        "example_prompt_kind", "example_tag"):
                self.assertIn(arg, params, f"{name} 에 {arg} 가 없다")

    def test_skill_tells_the_model_the_slots_exist(self):
        """빈 칸을 보여주지 않으면 채울 생각을 하지 않는다 — persona_observations 가 그랬다."""
        if not _SKILL.exists():
            self.skipTest("close-session skill 이 설치돼 있지 않다")
        text = _SKILL.read_text(encoding="utf-8")
        self.assertIn("engram_list_persona_examples", text)
        self.assertIn("capacity", text)
        self.assertTrue(re.search(r"example_\*", text), "example_* 작성 지시가 없다")


if __name__ == "__main__":
    unittest.main()
