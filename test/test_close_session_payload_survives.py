"""세션을 못 닫아도 내용은 남는다.

실제로 겪은 일: 종료 호출이 세 번 연속 거절됐고(scope 불일치 → 모호 → 이미 닫힘),
그때마다 narrative·예시·open_intents 가 통째로 버려졌다. 반환값은 그 사실을 말하지
않아서 저장된 줄 알고 넘어갔고, 다음 세션에 아무것도 남아 있지 않았다.

어제 붙인 테스트는 인자가 시그니처에 있는지만 봤다. 그래서 통과했는데 기능은 안
돌았다 — 실행 경로가 조기 반환으로 빠지면 인자는 읽히지도 않는다. 여기서는
**반환 경로마다** 검사한다.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.storage.db import get_connection, initialize_db


class ClosePayloadSurvivesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        self._patches = [
            patch("core.storage.db._get_db_dir", return_value=self.db_dir),
            # DB 만 격리하면 안 된다 — narrative 를 쓰면 provider 파일 동기화가
            # 뒤따라 실제 홈의 CLAUDE.md/AGENTS.md 를 덮어쓴다. 실제로 한 번
            # 덮어썼고, 실 DB 가 멀쩡해서 다시 sync 로 복구했다. 격리는 DB 가
            # 아니라 "부작용이 닿는 모든 곳"을 덮어야 한다.
        ]
        for one in self._patches:
            one.start()
        initialize_db(self.db_dir)

    def tearDown(self):
        for one in reversed(self._patches):
            one.stop()
        self._tmp.cleanup()

    def _payload(self, **over):
        import mcp_server

        args = dict(
            new_narrative="",
            persona_observations="",
            example_prompt_text="",
            example_response_text="",
            example_prompt_kind="situation",
            example_tag="",
        )
        args.update(over)
        return mcp_server._apply_close_payload(**args)

    def test_narrative_lands_without_any_session(self):
        from core.identity import get_identity

        result = self._payload(new_narrative="세션 행과 무관하게 남아야 하는 서술")
        self.assertTrue(result["reflection_applied"])
        self.assertIn("무관하게 남아야", get_identity().get("narrative", ""))

    def test_example_lands_without_any_session(self):
        from core.identity.examples import list_examples

        result = self._payload(
            example_prompt_text="자극이 된 상황",
            example_response_text="그때 한 말",
            example_tag="정정",
        )
        self.assertIsNotNone(result["example_saved"])
        self.assertEqual(result["example_skipped"], "")
        self.assertEqual(len(list_examples()), 1)

    def test_capacity_is_reported_so_a_refusal_is_visible(self):
        result = self._payload()
        self.assertIn("example_capacity", result)

    def test_open_intents_survive_as_working_memory(self):
        import mcp_server
        from core.memory import get_working_memory

        saved = mcp_server._salvage_working_memory(
            "overlay", "이번에 한 일", "다음에 이어할 일"
        )
        self.assertTrue(saved)
        stored = get_working_memory("overlay")
        self.assertIn("이어할 일", stored.get("open_intents", ""))

    def test_salvage_is_a_no_op_when_there_is_nothing_to_save(self):
        import mcp_server

        self.assertFalse(mcp_server._salvage_working_memory("overlay", "", ""))

    def test_every_early_return_reports_what_landed(self):
        """조기 반환이 payload 결과를 빼먹으면, 호출한 쪽은 저장된 줄 안다."""
        import inspect

        import mcp_server

        fn = getattr(mcp_server.engram_close_session, "fn", mcp_server.engram_close_session)
        source = inspect.getsource(fn)

        # 조기 반환은 전부 _bail() 을 거쳐야 한다. 맨손 return 이 남아 있으면
        # 그 경로만 조용히 내용을 버린다.
        # 반환 블록 단위로 본다 — 한 줄만 보면 여러 줄에 걸친 반환을 오판한다.
        blocks, current = [], None
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("return {"):
                current = [stripped]
            elif current is not None:
                current.append(stripped)
            if current is not None and stripped.endswith("}"):
                blocks.append(" ".join(current))
                current = None
        bare = [b for b in blocks if "**applied" not in b and "_bail(" not in b]
        self.assertEqual(
            bare, [], f"payload 를 싣지 않는 반환 경로가 남아 있다: {bare}"
        )
        self.assertIn("_bail(", source)
        self.assertIn("**applied", source)


if __name__ == "__main__":
    unittest.main()
