"""아카이브 충실성 회귀 — 멱등 적재, 원 시각 보존, 사람 발화 판정.

배경: 아카이브(2026-09-12)는 STM 용 transcript 캡처(2026-07-13) 파이프에 얹혀
있었다. STM 에겐 흠이 아니던 재적재 중복·적재 시각 기록·SDK 사용자 발화 드롭이
무손실 아카이브에서는 전부 결함이었다. 여기 테스트는 그 세 가지가 되돌아오지
않게 막는다.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.memory.store import _normalize_source_ts
from core.memory.transcript_capture import (
    extract_turn,
    extract_turn_record,
    find_active_transcript_any,
)
from core.storage.archive import append_turn, get_archive_path, initialize_archive


def _user_line(text, *, origin=..., prompt_source=None, is_meta=False, uuid="u-1"):
    obj = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-09-16T05:42:26.269Z",
        "sessionId": "sess-1",
        "message": {"content": text},
    }
    if origin is not ...:
        obj["origin"] = origin
    if prompt_source is not None:
        obj["promptSource"] = prompt_source
    if is_meta:
        obj["isMeta"] = True
    return json.dumps(obj)


class HumanPromptJudgementTests(unittest.TestCase):
    def test_origin_human_is_kept(self):
        line = _user_line("안녕", origin={"kind": "human"})
        self.assertEqual(extract_turn(line), ("user", "안녕"))

    def test_sdk_prompt_without_origin_is_kept(self):
        """말풍선(SDK) 경유 턴에는 origin 이 아예 없다. 옛 판정은 이걸 전부 버렸다."""
        line = _user_line("말풍선이 너무 작은데?", prompt_source="sdk")
        self.assertEqual(extract_turn(line), ("user", "말풍선이 너무 작은데?"))

    def test_typed_prompt_without_origin_is_kept(self):
        line = _user_line("커밋함?", prompt_source="typed")
        self.assertEqual(extract_turn(line), ("user", "커밋함?"))

    def test_unknown_prompt_source_is_dropped(self):
        """hook 주입·task-notification 처럼 표지가 없는 건 사람 발화가 아니다."""
        self.assertIsNone(extract_turn(_user_line("주입된 알림")))
        self.assertIsNone(extract_turn(_user_line("알림", prompt_source="task-notification")))

    def test_system_injected_text_is_dropped_even_with_human_source(self):
        for text in (
            "The previous response failed to produce a valid tool call. Please retry.",
            "[Image: original 2576x1408, displayed at 2000x1093.]",
        ):
            with self.subTest(text=text[:30]):
                self.assertIsNone(extract_turn(_user_line(text, prompt_source="sdk")))

    def test_meta_line_is_dropped(self):
        line = _user_line("리마인더", origin={"kind": "human"}, is_meta=True)
        self.assertIsNone(extract_turn(line))


class TurnRecordTests(unittest.TestCase):
    def test_record_carries_upstream_identity_and_time(self):
        line = _user_line("원문", prompt_source="typed", uuid="abc-123")
        rec = extract_turn_record(line)
        self.assertEqual(rec["uuid"], "abc-123")
        self.assertEqual(rec["timestamp"], "2026-09-16T05:42:26.269Z")
        self.assertEqual(rec["session_uuid"], "sess-1")
        self.assertEqual(rec["role"], "user")

    def test_dropped_line_yields_no_record(self):
        self.assertIsNone(extract_turn_record(_user_line("주입")))


class TimestampNormalizationTests(unittest.TestCase):
    def test_iso_utc_becomes_local_archive_format(self):
        got = _normalize_source_ts("2026-09-16T05:42:26.269Z")
        self.assertRegex(got, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_blank_and_garbage_fall_back_to_none(self):
        for v in ("", None, "not-a-time"):
            with self.subTest(v=v):
                self.assertIsNone(_normalize_source_ts(v))


class ArchiveIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        initialize_archive(self.dir)

    def _rows(self):
        conn = sqlite3.connect(str(get_archive_path(self.dir)))
        try:
            return conn.execute(
                "SELECT role, content, ts, source_uuid FROM turns ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def test_same_source_uuid_inserts_once(self):
        for _ in range(3):
            append_turn(1, "user", "같은 턴", scope_key="overlay",
                        db_dir=self.dir, source_uuid="u-9", ts="2026-09-16 14:42:26")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "2026-09-16 14:42:26")

    def test_duplicate_returns_none_but_is_not_an_error(self):
        first = append_turn(1, "user", "턴", scope_key="overlay",
                            db_dir=self.dir, source_uuid="u-1")
        second = append_turn(1, "user", "턴", scope_key="overlay",
                             db_dir=self.dir, source_uuid="u-1")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_rows_without_uuid_are_not_collapsed(self):
        """상류가 신원을 못 주면 중복 제거 대상이 아니다 — 진짜로 두 번 말했을 수 있다."""
        append_turn(1, "user", "ㅇㅇ", scope_key="overlay", db_dir=self.dir)
        append_turn(1, "user", "ㅇㅇ", scope_key="overlay", db_dir=self.dir)
        self.assertEqual(len(self._rows()), 2)

    def test_fts_index_matches_inserted_row_once(self):
        for _ in range(2):
            append_turn(1, "user", "임베딩 파이프라인", scope_key="overlay",
                        db_dir=self.dir, source_uuid="u-fts")
        conn = sqlite3.connect(str(get_archive_path(self.dir)))
        try:
            hits = conn.execute(
                "SELECT COUNT(*) FROM turns_fts WHERE turns_fts MATCH ?", ("임베딩",)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(hits, 1)

    def test_migration_adds_column_to_legacy_table(self):
        """컬럼 없는 옛 DB 에 붙어도 초기화가 깨지지 않아야 한다."""
        legacy = Path(tempfile.mkdtemp())
        conn = sqlite3.connect(str(legacy / "archive.db"))
        with conn:
            conn.execute(
                "CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,"
                " scope_key TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, content TEXT NOT NULL,"
                " ts TEXT NOT NULL DEFAULT (datetime('now','localtime')))"
            )
            conn.execute("INSERT INTO turns (session_id, role, content) VALUES (1,'user','옛 행')")
        conn.close()

        initialize_archive(legacy)
        rowid = append_turn(1, "user", "새 행", scope_key="overlay",
                            db_dir=legacy, source_uuid="u-new")
        self.assertIsNotNone(rowid)

        conn = sqlite3.connect(str(legacy / "archive.db"))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
            self.assertIn("source_uuid", cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 2)
        finally:
            conn.close()


class ActiveTranscriptScopeTests(unittest.TestCase):
    def test_scans_every_project_not_just_one(self):
        home = Path(tempfile.mkdtemp())
        projects = home / "projects"
        (projects / "proj-a").mkdir(parents=True)
        (projects / "proj-b").mkdir(parents=True)
        old = projects / "proj-a" / "a.jsonl"
        new = projects / "proj-b" / "b.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        new.write_text("{}\n", encoding="utf-8")
        import os, time
        past = time.time() - 600
        os.utime(old, (past, past))

        self.assertEqual(find_active_transcript_any(home), new)

    def test_missing_home_returns_none(self):
        self.assertIsNone(find_active_transcript_any(Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
