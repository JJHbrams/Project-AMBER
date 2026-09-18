"""engram 세션을 LLM 대화 단위로 묶는 회귀.

2026-09-18 실측: LLM 세션 13개(session-agent-orchestration 13,007줄,
ProjectIntelContunuum 6,341줄, TruviewCADMOM 4,365줄 …)의 메시지가 engram
세션 654 하나에 들어갔다. `/stm/message` 가 session_id 없이 scope_key 만
보내고, 그걸 scope 안 "가장 최근 열린 세션"으로 풀었기 때문이다.

결과로 체크포인트도 하나, 요약도 하나, 프로젝트도 뒤섞였다. 세션이 대화마다
갈리면 그 세 가지가 자연히 갈린다.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


class NativeSessionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        from core.storage.db import initialize_db

        self._patch = mock.patch("core.storage.db._get_db_dir", return_value=self.dir)
        self._patch.start()
        initialize_db(self.dir)

    def tearDown(self):
        self._patch.stop()

    def _resolve(self, native, scope="overlay"):
        from core.memory.store import resolve_session_id_by_native

        return resolve_session_id_by_native(native, scope)

    def test_same_conversation_reuses_one_session(self):
        first = self._resolve("llm-aaa")
        second = self._resolve("llm-aaa")
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_different_conversations_get_different_sessions(self):
        a = self._resolve("llm-aaa")
        b = self._resolve("llm-bbb")
        self.assertNotEqual(a, b)

    def test_blank_native_id_resolves_to_nothing(self):
        """상류가 못 주면 호출부가 예전 scope 경로로 떨어진다."""
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))

    def test_native_id_is_stored_on_the_session(self):
        sid = self._resolve("llm-ccc")
        conn = sqlite3.connect(str(self.dir / "engram.db"))
        try:
            got = conn.execute(
                "SELECT native_session_id FROM sessions WHERE id=?", (sid,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(got, "llm-ccc")

    def test_a_closed_session_does_not_get_reused(self):
        sid = self._resolve("llm-ddd")
        conn = sqlite3.connect(str(self.dir / "engram.db"))
        with conn:
            conn.execute("UPDATE sessions SET ended_at=datetime('now') WHERE id=?", (sid,))
        conn.close()
        self.assertNotEqual(self._resolve("llm-ddd"), sid)


class MultipleCheckpointCandidateTests(unittest.TestCase):
    """후보가 최신 1개로 제한되면 갈린 세션 대부분이 마감을 못 받는다."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        from core.storage.db import initialize_db

        self._patch = mock.patch("core.storage.db._get_db_dir", return_value=self.dir)
        self._patch.start()
        initialize_db(self.dir)
        self.conn = sqlite3.connect(str(self.dir / "engram.db"))

    def tearDown(self):
        self.conn.close()
        self._patch.stop()

    def _session_with_messages(self, native, *, turns=6, idle_minutes=60):
        stamp = (datetime.now() - timedelta(minutes=idle_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sessions (scope_key, native_session_id) VALUES ('overlay', ?)",
                (native,),
            )
            sid = cur.lastrowid
            for i in range(turns):
                self.conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "user", f"질문 {i}", stamp),
                )
                self.conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", f"답변 {i}", stamp),
                )
        return sid

    def test_every_idle_session_becomes_a_candidate(self):
        from core.graph.semantic.stm_promoter import _get_auto_checkpoint_candidates

        ids = {self._session_with_messages(f"llm-{n}") for n in "abc"}
        got = _get_auto_checkpoint_candidates("overlay", idle_seconds=1800, min_user_turns=5)
        self.assertEqual({c["session_id"] for c in got}, ids)

    def test_a_busy_session_is_not_a_candidate(self):
        from core.graph.semantic.stm_promoter import _get_auto_checkpoint_candidates

        self._session_with_messages("llm-busy", idle_minutes=1)
        self.assertEqual(_get_auto_checkpoint_candidates("overlay", idle_seconds=1800, min_user_turns=5), [])

    def test_a_short_session_is_not_a_candidate(self):
        from core.graph.semantic.stm_promoter import _get_auto_checkpoint_candidates

        self._session_with_messages("llm-short", turns=2)
        self.assertEqual(_get_auto_checkpoint_candidates("overlay", idle_seconds=1800, min_user_turns=5), [])

    def test_a_long_abandoned_session_is_not_a_candidate(self):
        """세션은 binding 없이 안 닫혀서 몇 주씩 열려 있다. 상한이 없으면
        폴링마다 묵은 세션이 쏟아진다 — 2026-09-18 에 9월 6·7·12일자
        체크포인트가 1분 간격으로 연달아 적혔다."""
        from core.graph.semantic.stm_promoter import _get_auto_checkpoint_candidates

        self._session_with_messages("llm-abandoned", idle_minutes=60 * 24 * 30)
        self.assertEqual(_get_auto_checkpoint_candidates("overlay", idle_seconds=1800, min_user_turns=5), [])

    def test_recently_quiet_sessions_come_first(self):
        """갓 끝난 대화가 묵은 것 뒤로 밀리면 안 된다."""
        from core.graph.semantic.stm_promoter import _get_auto_checkpoint_candidates

        older = self._session_with_messages("llm-older", idle_minutes=300)
        fresh = self._session_with_messages("llm-fresh", idle_minutes=35)
        got = _get_auto_checkpoint_candidates("overlay", idle_seconds=1800, min_user_turns=5)
        self.assertEqual([c["session_id"] for c in got], [fresh, older])


if __name__ == "__main__":
    unittest.main()
