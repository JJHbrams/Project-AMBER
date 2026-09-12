"""대화 원문 아카이브 — 무손실 적재와 앵커/스크롤 검색."""

import pytest

from core.storage import archive


@pytest.fixture()
def db_dir(tmp_path):
    archive._initialized_dirs.clear()
    yield tmp_path
    archive._initialized_dirs.clear()


def _seed(db_dir, turns, session_id=1, scope_key="overlay"):
    return [
        archive.append_turn(session_id, role, text, scope_key=scope_key, db_dir=db_dir)
        for role, text in turns
    ]


def test_append_keeps_full_text(db_dir):
    """sanitize(4000) 로 잘리는 messages 와 달리 원문 길이를 보존해야 한다."""
    long_text = "가" * 12000
    turn_id = archive.append_turn(1, "assistant", long_text, db_dir=db_dir)

    rows = archive.scroll_turns(turn_id, before=0, after=0, db_dir=db_dir)
    assert len(rows[0]["content"]) == 12000
    assert "truncated" not in rows[0]["content"]


def test_append_skips_blank(db_dir):
    assert archive.append_turn(1, "user", "   ", db_dir=db_dir) is None
    assert archive.archive_stats(db_dir=db_dir)["turns"] == 0


def test_search_matches_korean_with_josa(db_dir):
    """교착어 검증 — 조사가 붙은 어절도 어간으로 찾혀야 한다.

    기본 unicode61 토크나이저는 '임베딩이'를 한 토큰으로 잡아 '임베딩'에 걸리지
    않는다. trigram 을 고른 이유가 이것이라서 회귀로 박아둔다.
    """
    _seed(db_dir, [("assistant", "임베딩이 284MB를 쓰고 있어")])

    hits = archive.search_turns("임베딩", db_dir=db_dir)
    assert len(hits) == 1
    assert hits[0]["role"] == "assistant"


def test_search_treats_query_as_literal(db_dir):
    """사용자 입력이 FTS 연산자로 해석되면 안 된다."""
    _seed(db_dir, [("user", 'he said "OR 1=1" out loud')])

    hits = archive.search_turns('"OR 1=1"', db_dir=db_dir)
    assert len(hits) == 1
    assert archive.search_turns("nonexistent phrase", db_dir=db_dir) == []


def test_search_short_query_falls_back_to_like(db_dir):
    """2자 이하는 trigram 인덱스를 못 타므로 LIKE 경로로 내려간다."""
    _seed(db_dir, [("user", "DB 얘기 좀 하자")])

    hits = archive.search_turns("DB", db_dir=db_dir)
    assert len(hits) == 1


def test_search_filters_by_scope(db_dir):
    archive.append_turn(1, "user", "공용 브랜치 얘기", scope_key="overlay", db_dir=db_dir)
    archive.append_turn(2, "user", "공용 브랜치 얘기", scope_key="discord", db_dir=db_dir)

    hits = archive.search_turns("공용 브랜치", scope_key="discord", db_dir=db_dir)
    assert [h["session_id"] for h in hits] == [2]


def test_scroll_expands_around_anchor(db_dir):
    ids = _seed(
        db_dir,
        [("user", f"턴 {i}") for i in range(10)],
    )
    anchor = ids[5]

    rows = archive.scroll_turns(anchor, before=2, after=2, db_dir=db_dir)

    assert [r["content"] for r in rows] == ["턴 3", "턴 4", "턴 5", "턴 6", "턴 7"]


def test_scroll_stays_within_session(db_dir):
    """다른 세션의 턴이 앞뒤로 섞여 들어오면 맥락이 오염된다."""
    _seed(db_dir, [("user", "다른 세션 앞")], session_id=1)
    anchor = archive.append_turn(2, "user", "앵커", db_dir=db_dir)
    _seed(db_dir, [("user", "다른 세션 뒤")], session_id=1)

    rows = archive.scroll_turns(anchor, before=5, after=5, db_dir=db_dir)

    assert [r["content"] for r in rows] == ["앵커"]


def test_scroll_unknown_anchor_returns_empty(db_dir):
    assert archive.scroll_turns(99999, db_dir=db_dir) == []


def test_scroll_refuses_anchor_from_another_scope(db_dir):
    anchor = archive.append_turn(1, "user", "private project turn", scope_key="private", db_dir=db_dir)
    assert archive.scroll_turns(anchor, scope_key="overlay", db_dir=db_dir) == []
    assert [row["content"] for row in archive.scroll_turns(anchor, scope_key="private", db_dir=db_dir)] == ["private project turn"]


@pytest.mark.parametrize("session_id", [7, None])
def test_scroll_filters_neighbors_from_another_scope(db_dir, session_id):
    """같은/NULL 세션 ID라도 다른 scope의 이웃 턴은 노출하지 않는다."""
    archive.append_turn(session_id, "user", "다른 scope 앞", scope_key="private", db_dir=db_dir)
    anchor = archive.append_turn(session_id, "user", "공개 앵커", scope_key="overlay", db_dir=db_dir)
    archive.append_turn(session_id, "assistant", "다른 scope 뒤", scope_key="private", db_dir=db_dir)

    rows = archive.scroll_turns(
        anchor, before=5, after=5, scope_key="overlay", db_dir=db_dir
    )

    assert [row["content"] for row in rows] == ["공개 앵커"]


def test_stats_reports_volume(db_dir):
    _seed(db_dir, [("user", "12345"), ("assistant", "678901234")])

    stats = archive.archive_stats(db_dir=db_dir)

    assert stats["turns"] == 2
    assert stats["content_bytes"] == 14
    assert stats["sessions"] == 1
    assert stats["file_bytes"] > 0


def test_stats_on_missing_archive(tmp_path):
    stats = archive.archive_stats(db_dir=tmp_path / "nope")
    assert stats["turns"] == 0
    assert stats["file_bytes"] == 0


def test_delete_keeps_fts_in_sync(db_dir):
    """external-content FTS 는 트리거가 없으면 유령 히트를 남긴다."""
    turn_id = archive.append_turn(1, "user", "지워질 문장이다", db_dir=db_dir)
    conn = archive.get_archive_connection(db_dir)
    with conn:
        conn.execute("DELETE FROM turns WHERE id = ?", (turn_id,))
    conn.close()

    assert archive.search_turns("지워질 문장", db_dir=db_dir) == []
