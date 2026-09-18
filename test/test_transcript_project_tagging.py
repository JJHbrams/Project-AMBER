"""프로젝트 태깅 — 저장은 한 바구니, 조회만 좁힌다.

스코프로 나누자는 제안이 반복해서 나오고, 나눌 때마다 새 바구니가 빈 채로 시작해
과거와 끊긴다(실측: overlay 50,379메시지 / 그 밖의 모든 스코프 합계 두 자릿수).
그래서 여기서 검사하는 첫 번째 불변식은 "태그를 붙여도 scope_key 는 그대로"다.
"""

import tempfile
import unittest
from pathlib import Path

from core.context.project_scope import resolve_project_tag
from core.storage.archive import (
    append_turn,
    get_archive_connection,
    initialize_archive,
    project_counts,
    search_turns,
)


class TranscriptProjectTaggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        initialize_archive(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, text, project_key, scope_key="overlay"):
        return append_turn(
            None, "assistant", text, scope_key=scope_key,
            project_key=project_key, db_dir=self.dir,
        )

    def test_tag_does_not_change_where_the_turn_is_stored(self):
        """태그는 라벨이다. 저장 위치를 가르면 연속성이 끊긴다."""
        self._add("첫 번째 프로젝트 이야기", "alpha-1111")
        self._add("두 번째 프로젝트 이야기", "beta-2222")

        conn = get_archive_connection(self.dir)
        try:
            scopes = [r[0] for r in conn.execute("SELECT DISTINCT scope_key FROM turns")]
        finally:
            conn.close()
        self.assertEqual(scopes, ["overlay"], "태그가 scope_key 를 갈랐다")

    def test_search_narrows_by_tag_but_finds_everything_without_one(self):
        self._add("공통 단어가 들어간 알파 문장", "alpha-1111")
        self._add("공통 단어가 들어간 베타 문장", "beta-2222")

        everything = search_turns("공통 단어", db_dir=self.dir)
        self.assertEqual(len(everything), 2)

        alpha = search_turns("공통 단어", project_key="alpha-1111", db_dir=self.dir)
        self.assertEqual([r["project_key"] for r in alpha], ["alpha-1111"])

    def test_untagged_turns_stay_searchable(self):
        """태그 도입 이전 턴은 미분류다. 안 붙었다고 사라지면 안 된다."""
        self._add("태그 없는 옛날 문장", "")
        hits = search_turns("옛날 문장", db_dir=self.dir)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["project_key"], "")

    def test_counts_report_the_unclassified_bucket_too(self):
        """미분류 비율이 태그 품질의 지표다 — 숨기면 판정 실패가 안 보인다."""
        self._add("알파 하나", "alpha-1111")
        self._add("알파 둘", "alpha-1111")
        self._add("미분류 하나", "")

        counts = {r["project_key"]: r["turns"] for r in project_counts(db_dir=self.dir)}
        self.assertEqual(counts, {"alpha-1111": 2, "": 1})

    def test_migration_adds_the_column_to_an_existing_archive(self):
        """CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블에 컬럼을 더해주지 않는다."""
        conn = get_archive_connection(self.dir)
        with conn:
            conn.execute("DROP TABLE turns")
            conn.execute("""
                CREATE TABLE turns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  INTEGER,
                    scope_key   TEXT NOT NULL DEFAULT '',
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    ts          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    source_uuid TEXT
                )
            """)
            conn.execute(
                "INSERT INTO turns (scope_key, role, content) VALUES ('overlay','assistant','옛 턴')"
            )
        conn.close()

        initialize_archive(self.dir)

        conn = get_archive_connection(self.dir)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
            row = conn.execute("SELECT project_key, content FROM turns").fetchone()
        finally:
            conn.close()
        self.assertIn("project_key", cols)
        self.assertEqual(row["project_key"], "", "소급 추정 없이 미분류여야 한다")
        self.assertEqual(row["content"], "옛 턴")

    def test_remote_paths_get_grouped_instead_of_dropped(self):
        """리버스 터널 cwd 는 이 파일시스템에 없다. 버리면 원격 작업이 전부 미분류가 된다."""
        tag = resolve_project_tag("/home/someone/work/RemoteThing")
        self.assertEqual(tag, "remote:remotething")
        self.assertEqual(resolve_project_tag(""), "")

    def test_local_path_tag_matches_the_project_key(self):
        from core.context.project_scope import resolve_project_key

        here = str(Path(__file__).resolve().parent)
        self.assertEqual(resolve_project_tag(here), resolve_project_key(cwd=here))


if __name__ == "__main__":
    unittest.main()
