"""렌더 경로 — AC-4(기본 호출에는 예시가 안 붙는다) · AC-7(삭제가 즉시 반영된다)."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.storage.db import initialize_db


class PersonaExamplesRenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        self._patch = patch("core.storage.db._get_db_dir", return_value=self.db_dir)
        self._patch.start()
        initialize_db(self.db_dir)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_examples_are_opt_in(self):
        from core.identity import add_example, render_persona

        add_example("질문", "대답인데 길이를 채우려고 늘여 쓴 문장", tag="진단")
        persona = {"voice": "반말", "warmth": 0.5}

        self.assertNotIn("[examples]", render_persona(persona))
        self.assertIn("[examples]", render_persona(persona, include_examples=True))

    def test_initiative_uses_the_default(self):
        """자발 발화는 1회성 격리 호출이라 예시를 태울 자리가 아니다."""
        source = Path("overlay/bubble/initiative.py").read_text(encoding="utf-8")
        calls = re.findall(r"render_persona\([^)]*\)", source)
        self.assertTrue(calls, "initiative 에서 render_persona 호출을 못 찾았다")
        for call in calls:
            self.assertNotIn("include_examples", call)

    def test_anchor_and_growth_are_additive(self):
        """user.yaml 의 fewshot 은 지향, 테이블은 성장 — 한쪽이 다른 쪽을 지우면 안 된다."""
        from core.identity import add_example, render_persona

        persona = {"voice": "반말", "fewshot": "U: 수동\nA: 사람이 박은 앵커"}
        self.assertIn("사람이 박은 앵커", render_persona(persona, include_examples=True))

        add_example("질문", "테이블에서 온 대답인데 길이를 채운다", tag="진단")
        rendered = render_persona(persona, include_examples=True)
        self.assertIn("사람이 박은 앵커", rendered)
        self.assertIn("테이블에서 온 대답", rendered)
        self.assertLess(
            rendered.index("사람이 박은 앵커"), rendered.index("테이블에서 온 대답"),
            "앵커가 먼저 와야 한다",
        )

    def test_fewshot_only_switch_drops_the_table(self):
        from core.identity import add_example, render_persona

        add_example("질문", "테이블에서 온 대답인데 길이를 채운다", tag="진단")
        persona = {"voice": "반말", "fewshot": "U: 수동\nA: 사람이 박은 앵커", "fewshot_only": True}
        rendered = render_persona(persona, include_examples=True)
        self.assertIn("사람이 박은 앵커", rendered)
        self.assertNotIn("테이블에서 온 대답", rendered)

    def test_anchor_cannot_starve_the_budget_silently(self):
        """앵커가 예산을 다 먹으면 성장은 못 붙되, 앵커는 온전히 남는다."""
        from core.identity import add_example, render_persona
        from core.identity.examples import RENDER_CHAR_BUDGET

        add_example("질문", "테이블에서 온 대답인데 길이를 채운다", tag="진단")
        huge = "".join(chr(0xac00 + (i * 7) % 2000) for i in range(RENDER_CHAR_BUDGET))
        rendered = render_persona({"voice": "반말", "fewshot": huge}, include_examples=True)
        self.assertIn(huge, rendered)
        self.assertNotIn("테이블에서 온 대답", rendered)

    def test_switch_is_not_blended_by_reflection(self):
        """스위치는 평균낼 수 있는 값이 아니다 — EMA 가 0.7 같은 걸 만들면 안 된다."""
        from core.identity.service import _merge_persona

        merged = _merge_persona({"fewshot_only": True, "warmth": 0.8}, {"fewshot_only": False})
        self.assertIs(merged["fewshot_only"], False)
        merged = _merge_persona({"fewshot_only": True}, {"warmth": 0.2})
        self.assertIs(merged["fewshot_only"], True)

    def test_retire_and_delete_take_effect_on_next_render(self):
        from core.identity import (
            add_example,
            delete_example,
            render_persona,
            restore_example,
            retire_example,
        )

        row = add_example("질문", "사라져야 하는 대답인데 길이를 채운다", tag="진단")
        persona = {"voice": "반말"}
        self.assertIn("사라져야 하는", render_persona(persona, include_examples=True))

        self.assertTrue(retire_example(row["id"]))
        self.assertNotIn("사라져야 하는", render_persona(persona, include_examples=True))

        self.assertTrue(restore_example(row["id"]))
        self.assertIn("사라져야 하는", render_persona(persona, include_examples=True))

        self.assertTrue(delete_example(row["id"]))
        self.assertNotIn("[examples]", render_persona(persona, include_examples=True))


if __name__ == "__main__":
    unittest.main()
