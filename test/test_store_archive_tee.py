"""save_message 가 주입용 사본과 원문 아카이브를 동시에 남기는지 검증한다.

여기서 DB 경로는 반드시 모듈 속성을 직접 치환해서 격리한다. `ENGRAM_DB_DIR`
환경변수는 안전하지 않다 — `get_db_root_dir()` 의 우선순위가
user.config.yaml → env → project config 라서, 사용자 설정이 있으면 환경변수가
무시되고 테스트가 실제 DB에 쓴다.
"""

import pytest

from core.memory import store
from core.storage import archive
from core.storage import db as storage_db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    root = tmp_path / "engram-root"
    root.mkdir()
    monkeypatch.setattr(storage_db, "get_db_root_dir", lambda: str(root))
    monkeypatch.setattr(archive, "get_db_root_dir", lambda: str(root))
    archive._initialized_dirs.clear()
    storage_db.initialize_db()
    archive.initialize_archive()
    yield root
    archive._initialized_dirs.clear()


def test_archive_keeps_what_messages_truncates(isolated_db):
    """messages 는 4000자로 잘리고, 아카이브는 원문 길이를 지켜야 한다."""
    session_id = store.create_session(scope_key="overlay")
    original = "문장이 이어진다. " * 600  # 4000자 초과
    assert len(original) > 4000

    store.save_message(session_id, "assistant", original)

    conn = storage_db.get_connection()
    stored = conn.execute(
        "SELECT content FROM messages WHERE session_id=?", (session_id,)
    ).fetchone()["content"]
    conn.close()

    archived = archive.scroll_turns(1, before=0, after=0)[0]["content"]

    assert len(stored) < len(original)
    assert archived == original


def test_archive_redacts_secrets(isolated_db):
    """원문 보존이 비밀값 보존을 뜻하면 안 된다."""
    session_id = store.create_session(scope_key="overlay")

    store.save_message(session_id, "user", "토큰은 sk-ant-api03-FAKEKEY1234567890 이야")

    assert archive.search_turns("FAKEKEY") == []
    assert archive.search_turns("REDACTED")


def test_archive_inherits_session_scope(isolated_db):
    session_id = store.create_session(scope_key="discord")

    store.save_message(session_id, "user", "스코프가 따라와야 한다")

    hits = archive.search_turns("스코프가 따라와야", scope_key="discord")
    assert len(hits) == 1


def test_archive_failure_does_not_break_message_path(isolated_db, monkeypatch):
    """아카이브는 보조 계층이다 — 여기서 터져도 기억 기록은 계속돼야 한다."""
    session_id = store.create_session(scope_key="overlay")

    def boom(*args, **kwargs):
        raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(archive, "get_archive_connection", boom)

    store.save_message(session_id, "user", "이건 그래도 남아야 한다")

    conn = storage_db.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE session_id=?", (session_id,)
    ).fetchone()["c"]
    conn.close()
    assert count == 1


def test_closed_session_rejects_message(isolated_db):
    """세션 검증은 그대로여야 한다 — scope_key 조회로 바꾸면서 깨지기 쉬운 지점."""
    with pytest.raises(ValueError):
        store.save_message(999999, "user", "열린 세션이 아니다")
