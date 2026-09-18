"""persona_examples 저장소 — AC-1(멱등 마이그레이션) · AC-2(채굴은 쓰지 않음) · AC-3(태그 균형)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.storage.db import get_connection, initialize_db


def list_examples_ids():
    from core.identity.examples import list_examples
    return [r["id"] for r in list_examples()]


class PersonaExamplesStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        # ENGRAM_DB_DIR 은 격리 수단이 아니다 — user.config.yaml 이 환경변수를
        # 이겨서 실 DB 로 새어나간다(직접 확인, 2026-09-17). 해석 함수를 직접 막는다.
        self._patch = patch("core.storage.db._get_db_dir", return_value=self.db_dir)
        self._patch.start()
        initialize_db(self.db_dir)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _schema(self):
        conn = get_connection(self.db_dir)
        try:
            cols = [tuple(r) for r in conn.execute("PRAGMA table_info(persona_examples)")]
            count = conn.execute("SELECT COUNT(*) FROM persona_examples").fetchone()[0]
        finally:
            conn.close()
        return cols, count

    def test_migration_is_idempotent_on_fresh_and_existing_db(self):
        before = self._schema()
        self.assertTrue(before[0], "persona_examples 테이블이 만들어지지 않았다")
        from core.identity.examples import add_example

        add_example("질문", "대답인데 길이를 채우기 위한 문장이다", tag="진단")
        initialize_db(self.db_dir)   # 기존 DB 위에서 다시
        initialize_db(self.db_dir)
        after = self._schema()
        self.assertEqual(before[0], after[0])
        self.assertEqual(after[1], 1, "재초기화가 행을 지우거나 늘렸다")

    def test_mining_returns_candidates_without_writing(self):
        from core.identity import mining
        from core.storage.archive import initialize_archive, append_turn

        archive_dir = self.db_dir / "arc"
        initialize_archive(archive_dir)
        for role, text in (("user", "짧은 질문"), ("assistant", "답변인데 길이 필터의 하한 40자를 넘기려고 일부러 길게 늘여 쓴 문장이다. 이 정도면 통과한다")):
            append_turn(None, role, text, scope_key="overlay", db_dir=archive_dir)

        before = self._schema()[1]
        found = mining.mine_candidates(limit=5, db_dir=archive_dir)
        after = self._schema()[1]

        self.assertGreaterEqual(len(found), 1)
        self.assertEqual(before, after, "채굴이 persona_examples 에 행을 썼다")
        self.assertTrue(found[0]["needs_prompt_text"])
        self.assertEqual(found[0]["prompt_text"], "")
        self.assertEqual(found[0]["suggested_prompt_kind"], "situation")
    def test_render_balances_tags_and_respects_both_budgets(self):
        from core.identity.examples import (
            RENDER_CHAR_BUDGET, RENDER_LIMIT, TAG_CAP, add_example, render_examples,
        )

        tags = ("진단", "반박", "농담", "거절", "설명")
        for tag in tags:
            for n in range(5):
                add_example(f"{tag} 질문 {n}", f"{tag} 대답 {n} " + "내용" * 10, tag=tag)

        block = render_examples()
        pairs = [chunk for chunk in block.split("\n\n") if chunk.strip()]
        self.assertLessEqual(len(pairs), RENDER_LIMIT)
        self.assertLessEqual(len(block), RENDER_CHAR_BUDGET)
        for tag in tags:
            used = sum(1 for chunk in pairs if chunk.startswith(f"U: {tag} "))
            self.assertLessEqual(used, TAG_CAP, f"{tag} 가 상한을 넘겼다")

    def test_char_budget_cuts_before_count_limit(self):
        from core.identity.examples import RENDER_CHAR_BUDGET, RENDER_LIMIT, add_example, render_examples

        # 같은 글자 반복은 sanitize 의 노이즈 정리에 뭉개진다 — 길이를 남기려면 변주해야 한다.
        filler = "".join(chr(0xac00 + (i * 7) % 2000) for i in range(500))
        for n in range(RENDER_LIMIT):
            add_example(f"질문 {n}", "대답 " + filler, tag=f"t{n}")
        block = render_examples()
        self.assertLessEqual(len(block), RENDER_CHAR_BUDGET)
        self.assertLess(len([c for c in block.split("\n\n") if c.strip()]), RENDER_LIMIT)
        self.assertLess(len([c for c in block.split("\n\n") if c.strip()]), 8)

    def test_prompt_kinds_render_with_visible_labels(self):
        """상황·자료를 U: 로 위장하면 "사용자는 이런 말을 한다"를 가르치게 된다."""
        from core.identity.examples import add_example, render_examples

        add_example("찐빠나는 건 아니지?", "악영향 없음은 아니야", prompt_kind="user", tag="진단")
        add_example("스모크에서 pair가 어긋난 걸 발견", "채굴은 도는데 결과가 틀렸어", prompt_kind="situation", tag="정정")
        add_example("runtime_config.py:326 — user.config.yaml이 먼저", "ENGRAM_DB_DIR은 격리가 아니야", prompt_kind="source", tag="측정")

        block = render_examples()
        self.assertIn("U: 찐빠나는", block)
        self.assertIn("상황: 스모크에서", block)
        self.assertIn("자료: runtime_config", block)
        self.assertEqual(block.count("A: "), 3)

    def test_unknown_prompt_kind_is_rejected(self):
        from core.identity.examples import add_example

        with self.assertRaises(ValueError):
            add_example("자극", "응답인데 길이를 채운다", prompt_kind="narration")

    def test_default_kind_is_user(self):
        from core.identity.examples import add_example

        row = add_example("질문", "대답인데 길이를 채운다")
        self.assertEqual(row["prompt_kind"], "user")

    def test_legacy_schema_is_migrated_and_rows_survive(self):
        """초기 스키마가 이미 깔린 설치본 — IF NOT EXISTS 는 그걸 건너뛴다."""
        from core.storage.db import get_connection

        conn = get_connection(self.db_dir)
        with conn:
            conn.execute("DROP TABLE persona_examples")
            conn.execute("""
                CREATE TABLE persona_examples (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_text      TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    tag            TEXT NOT NULL DEFAULT '',
                    source_turn_id INTEGER,
                    weight         INTEGER NOT NULL DEFAULT 0,
                    active         INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute(
                "INSERT INTO persona_examples (user_text, assistant_text, tag) VALUES (?,?,?)",
                ("옛 질문", "옛 대답인데 길이를 채운다", "진단"),
            )
        conn.close()

        initialize_db(self.db_dir)

        conn = get_connection(self.db_dir)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(persona_examples)")]
            row = conn.execute("SELECT * FROM persona_examples").fetchone()
            leftovers = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'persona_examples_legacy'"
                )
            ]
        finally:
            conn.close()

        self.assertIn("prompt_kind", cols)
        self.assertNotIn("user_text", cols)
        self.assertEqual(row["prompt_text"], "옛 질문")
        self.assertEqual(row["response_text"], "옛 대답인데 길이를 채운다")
        self.assertEqual(row["prompt_kind"], "user")
        self.assertEqual(leftovers, [], "legacy 테이블이 남았다")

        initialize_db(self.db_dir)   # 두 번째 호출은 아무 일도 하지 않아야 한다
        from core.identity.examples import list_examples
        self.assertEqual(len(list_examples()), 1)

    def test_long_pair_is_saved_but_flagged(self):
        """거부하지 않는다 — 구조를 보여줘야 하는 예시는 길 수밖에 없다."""
        from core.identity.examples import PAIR_CHARS_ADVISED, add_example

        filler = "".join(chr(0xac00 + (i * 7) % 2000) for i in range(PAIR_CHARS_ADVISED))
        saved = add_example("자극", filler, tag="진단")
        self.assertIn("advice", saved)
        self.assertEqual(len(list_examples_ids()), 1)

        short = add_example("짧은 자극", "짧은 응답인데 권장 길이 안쪽이다", tag="진단")
        self.assertNotIn("advice", short)

    def test_shorter_pairs_fit_more_examples(self):
        """상한 재조정의 목적 — 같은 예산에 더 많은 다양성."""
        from core.identity.examples import add_example, render_examples

        for n in range(14):
            add_example(f"자극 {n}", f"응답 {n} 인데 대략 백 자 안쪽으로 짧게 유지한다", tag=f"t{n}")
        pairs = [c for c in render_examples().split("\n\n") if c.strip()]
        self.assertEqual(len(pairs), 12)
        self.assertEqual(len(pairs), 12)

    def test_manual_outranks_auto_in_rendering(self):
        """교정이 한 번의 호출로 끝나야 한다 — 넣으면 그게 자리를 가져간다."""
        from core.identity.examples import add_example, render_examples

        add_example("자극", "자동으로 쌓인 응답", tag="진단", origin="auto")
        add_example("자극", "사람이 넣은 응답", tag="진단", origin="manual")
        block = render_examples(limit=1, tag_cap=1)
        self.assertIn("사람이 넣은 응답", block)
        self.assertNotIn("자동으로 쌓인 응답", block)

    def test_origin_is_recorded_and_filterable(self):
        from core.identity.examples import add_example, list_examples

        add_example("자극", "자동 응답", tag="진단", origin="auto")
        add_example("자극", "수동 응답", tag="반박", origin="manual")
        self.assertEqual(
            [r["response_text"] for r in list_examples(origin="auto")], ["자동 응답"]
        )
        with self.assertRaises(ValueError):
            add_example("자극", "응답", origin="누군가")

    def test_tag_capacity_reports_remaining_room(self):
        from core.identity.examples import TAG_CAP, add_example, has_room_for_tag, tag_capacity

        for n in range(TAG_CAP):
            add_example(f"자극 {n}", f"응답 {n} 인데 길이를 채운다", tag="진단")
        self.assertEqual(tag_capacity()["진단"], 0)
        self.assertFalse(has_room_for_tag("진단"))
        self.assertTrue(has_room_for_tag("아직없는태그"))

    def test_origin_column_is_added_to_an_existing_table(self):
        """origin 없이 살던 설치본 — IF NOT EXISTS 는 그걸 건너뛴다."""
        from core.storage.db import get_connection

        conn = get_connection(self.db_dir)
        with conn:
            conn.execute("ALTER TABLE persona_examples RENAME TO tmp_old")
            conn.execute("""
                CREATE TABLE persona_examples (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_kind   TEXT NOT NULL DEFAULT 'user',
                    prompt_text   TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    tag           TEXT NOT NULL DEFAULT '',
                    source_turn_id INTEGER,
                    weight        INTEGER NOT NULL DEFAULT 0,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute("DROP TABLE tmp_old")
            conn.execute(
                "INSERT INTO persona_examples (prompt_text, response_text, tag)"
                " VALUES (?,?,?)", ("옛 자극", "옛 응답인데 길이를 채운다", "진단"),
            )
        conn.close()

        initialize_db(self.db_dir)

        from core.identity.examples import list_examples
        rows = list_examples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origin"], "manual")


if __name__ == "__main__":
    unittest.main()
