"""채굴 — AC-6(archive.db 에 쓰지 않는다)."""

import tempfile
import unittest
from pathlib import Path

from core.identity import mining
from core.storage.archive import (
    append_turn,
    get_archive_connection,
    get_archive_path,
    initialize_archive,
)


class PersonaExamplesMiningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        initialize_archive(self.dir)
        long_answer = "채굴 후보가 되려면 하한 40자를 넘겨야 하므로 일부러 길게 늘여 쓴 답변 문장이다"
        for n in range(3):
            append_turn(None, "user", f"질문 {n}", scope_key="overlay", db_dir=self.dir)
            append_turn(None, "assistant", f"{long_answer} {n}", scope_key="overlay", db_dir=self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _fingerprint(self):
        path = get_archive_path(self.dir)
        conn = get_archive_connection(self.dir)
        try:
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        finally:
            conn.close()
        return path.stat().st_size, turns

    def test_mining_does_not_write_to_the_archive(self):
        before = self._fingerprint()
        found = mining.mine_candidates(limit=10, db_dir=self.dir)
        self.assertEqual(len(found), 3)
        self.assertEqual(before, self._fingerprint(), "채굴이 archive.db 를 바꿨다")

    def test_mining_never_fabricates_a_pair(self):
        """archive 는 대화를 가르지 못한다 — 짝을 지어 주면 거짓말이 된다."""
        for candidate in mining.mine_candidates(limit=10, db_dir=self.dir):
            self.assertEqual(candidate["prompt_text"], "")
            self.assertTrue(candidate["needs_prompt_text"])
            self.assertIsNotNone(candidate["source_turn_id"])

    def test_query_narrows_by_full_text_search(self):
        # trigram 토크나이저라 2자 이하 토큰은 매칭되지 않는다. 공백은 AND 로
        # 갈리므로 "답변 문장" 처럼 2자 토큰이 섞이면 0건이 된다 — 한 덩어리로 준다.
        hits = mining.mine_candidates(query="문장이다", limit=10, db_dir=self.dir)
        self.assertEqual(len(hits), 3)
        self.assertEqual(mining.mine_candidates(query="존재하지않는말", limit=10, db_dir=self.dir), [])

    def test_short_utterances_are_filtered_out(self):
        append_turn(None, "assistant", "짧음", scope_key="overlay", db_dir=self.dir)
        texts = [c["response_text"] for c in mining.mine_candidates(limit=10, db_dir=self.dir)]
        self.assertNotIn("짧음", texts)


if __name__ == "__main__":
    unittest.main()
