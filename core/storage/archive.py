"""
대화 원문 아카이브 — 무손실 append-only 저장소.

DB 경로: <db.root_dir>\\archive.db (engram.db 와 물리적으로 분리)

engram.db 는 주입 경로(정체성·테마·directive·STM)를 위한 저장소라서 내용이
sanitize(max_length=4000) 로 잘린다. 요약본은 컨텍스트에 넣기 위한 것이고,
원문은 "그때 뭐라고 했지"를 되짚기 위한 것이라 용도가 다르다. 둘을 같은 테이블에
두면 한쪽 정책이 다른 쪽을 깎으므로 파일부터 나눈다.

분리 이유:
  1. 주입 경로 보호 — 아카이브 쓰기가 engram.db 의 WAL 락 경합에 끼어들지 않는다.
     overlay/claude/codex/discord 가 이미 engram.db 하나를 공유하고 있다.
  2. 보존 정책 분리 — 구조체는 자주 백업하고, 아카이브는 append-only 로 둔다.
  3. 실패 격리 — 아카이브가 망가져도 기억·정체성은 멀쩡해야 한다.

검색은 FTS5 trigram 이다. 기본 unicode61 토크나이저는 한국어에서 조사가 붙은
어절을 통째로 하나의 토큰으로 잡아서("임베딩이"), 어간("임베딩")으로는 아무것도
찾지 못한다. trigram 은 3자 단위 부분문자열 매칭이라 교착어에서도 동작한다.
대신 2자 이하 질의는 trigram 인덱스를 타지 못하므로 LIKE 스캔으로 내려간다.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config.runtime_config import get_db_root_dir

logger = logging.getLogger(__name__)

ARCHIVE_FILENAME = "archive.db"

# trigram 인덱스가 매칭할 수 있는 최소 질의 길이.
_MIN_TRIGRAM_QUERY = 3

# 한 번에 돌려주는 검색 결과 상한 — 되짚기용이지 덤프용이 아니다.
_MAX_SEARCH_LIMIT = 50

# 앵커 주변으로 확장할 수 있는 최대 턴 수.
_MAX_SCROLL_SPAN = 40


def get_archive_path(db_dir: "str | Path | None" = None) -> Path:
    root = Path(db_dir) if db_dir is not None else Path(get_db_root_dir())
    return root / ARCHIVE_FILENAME


def get_archive_connection(db_dir: "str | Path | None" = None) -> sqlite3.Connection:
    path = get_archive_path(db_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_initialized_dirs: "set[str]" = set()


def _ensure_initialized(db_dir: "str | Path | None" = None) -> None:
    """스키마를 프로세스당 경로별 1회만 만든다.

    engram.db 와 달리 아카이브는 부팅 경로에 초기화 훅이 없다. 첫 사용 시점에
    붙이되, 매 턴마다 executescript 를 돌리지 않도록 경로 단위로 기억한다.
    """
    key = str(get_archive_path(db_dir))
    if key in _initialized_dirs:
        return
    initialize_archive(db_dir)
    _initialized_dirs.add(key)


def initialize_archive(db_dir: "str | Path | None" = None) -> None:
    """최초 1회 테이블·FTS 인덱스 생성. 멱등."""
    conn = get_archive_connection(db_dir)
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  INTEGER,
                    scope_key   TEXT NOT NULL DEFAULT '',
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    ts          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    -- 상류(transcript)가 주는 줄 단위 고유 id. 있으면 재적재해도
                    -- 한 번만 들어간다. 상류가 못 주면 NULL 이고, 그때는 중복을
                    -- 막을 방법이 없다는 뜻이다(UNIQUE 는 NULL 을 서로 다르게 본다).
                    source_uuid TEXT,

                    -- 어느 프로젝트에서 일어난 턴인가. **스코프가 아니라 태그다.**
                    -- 저장은 연속체 한 바구니에 그대로 하고, 조회할 때만 좁힌다.
                    -- 스코프로 나누면 프로젝트별로 깔끔해 보이는 대신 새 바구니가
                    -- 빈 채로 시작해 과거와 끊긴다(실측 overlay 50,379메시지).
                    -- 비어 있으면 미분류다 — 판정에 실패한 것이지 틀린 것이 아니다.
                    project_key TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session
                    ON turns(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_turns_scope_ts
                    ON turns(scope_key, ts);
                -- source_uuid UNIQUE 인덱스는 아래 마이그레이션에서 만든다.
                -- 기존 DB 에는 아직 컬럼이 없어서 여기서 만들면 스크립트가 통째로 깨진다.

                -- external content: 원문을 두 벌 들고 있지 않도록 turns 를 참조만 한다.
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    content,
                    content='turns',
                    content_rowid='id',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
                    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
                END;
                """
            )

        # 기존 DB 마이그레이션 — CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블에
        # 컬럼을 더해주지 않는다. 컬럼이 없으면 붙이고 인덱스도 뒤따라 만든다.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        with conn:
            if "source_uuid" not in cols:
                conn.execute("ALTER TABLE turns ADD COLUMN source_uuid TEXT")
                logger.info("archive: source_uuid 컬럼 마이그레이션 완료")
            if "project_key" not in cols:
                # 기존 턴은 cwd 기록이 없어 소급 판정이 불가능하다. 빈 값(미분류)으로
                # 둔다 — 추정해서 채우면 틀린 태그가 되고, 그게 빈 태그보다 나쁘다.
                conn.execute("ALTER TABLE turns ADD COLUMN project_key TEXT NOT NULL DEFAULT ''")
                logger.info("archive: project_key 컬럼 마이그레이션 완료")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_project ON turns(project_key, id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_source_uuid "
                "ON turns(source_uuid) WHERE source_uuid IS NOT NULL"
            )
    finally:
        conn.close()


def append_turn(
    session_id: Optional[int],
    role: str,
    content: str,
    *,
    scope_key: str = "",
    db_dir: "str | Path | None" = None,
    source_uuid: Optional[str] = None,
    ts: Optional[str] = None,
    project_key: str = "",
) -> Optional[int]:
    """원문 턴을 그대로 적재한다. 자르지 않는다.

    빈 내용은 저장하지 않는다. 실패해도 예외를 올리지 않고 None 을 돌려준다 —
    아카이브는 보조 계층이고, 여기서 터져서 대화 기록 경로를 막으면 안 된다.

    - source_uuid: 상류가 주는 줄 단위 고유 id. 주면 재적재해도 한 번만 들어간다.
      상류를 여러 번 다시 읽는 건 사고가 아니라 정상 동작(overlay 재시작마다
      offset 이 0 으로 돌아간다)이라, 멱등성은 여기서 보장해야 한다.
    - ts: 실제 발화 시각. 안 주면 적재 시각이 들어가는데, 그 값은 "언제 읽었나"지
      "언제 말했나"가 아니다. 되짚기용 아카이브에서 시간축이 틀리면 쓸모가 없다.
    - project_key: 조회용 태그. 저장 위치는 바뀌지 않는다 — 어느 프로젝트에서
      말했든 같은 바구니에 들어가고, 이 값은 나중에 좁혀 볼 때만 쓴다.
    """
    if not content or not content.strip():
        return None
    try:
        _ensure_initialized(db_dir)
        conn = get_archive_connection(db_dir)
    except Exception:
        logger.warning("archive: 연결 실패 — 턴을 건너뛴다", exc_info=True)
        return None
    try:
        with conn:
            if ts:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO turns (session_id, scope_key, role, content, source_uuid, ts, project_key) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (session_id, str(scope_key or ""), str(role or ""), content, source_uuid, ts,
                     str(project_key or "")),
                )
            else:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO turns (session_id, scope_key, role, content, source_uuid, project_key) "
                    "VALUES (?,?,?,?,?,?)",
                    (session_id, str(scope_key or ""), str(role or ""), content, source_uuid,
                     str(project_key or "")),
                )
            # source_uuid 가 겹쳐 무시된 경우 rowcount 가 0 이다 — 중복은 실패가 아니다.
            return int(cur.lastrowid) if cur.rowcount else None
    except sqlite3.Error:
        logger.warning("archive: 턴 적재 실패", exc_info=True)
        return None
    finally:
        conn.close()


def _row_to_turn(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "scope_key": row["scope_key"],
        "role": row["role"],
        "ts": row["ts"],
        "content": row["content"],
        "project_key": row["project_key"] if "project_key" in row.keys() else "",
    }


def search_turns(
    query: str,
    *,
    limit: int = 10,
    scope_key: Optional[str] = None,
    project_key: Optional[str] = None,
    db_dir: "str | Path | None" = None,
) -> List[Dict[str, Any]]:
    """원문 전문검색. 앵커 후보 목록을 돌려준다.

    내용 전체가 아니라 매칭 구간 snippet 만 싣는다. 앞뒤 흐름이 필요하면
    반환된 id 로 scroll_turns() 를 부른다 — 검색과 확장은 별개 연산이다.
    """
    text = (query or "").strip()
    if not text:
        return []
    limit = max(1, min(int(limit), _MAX_SEARCH_LIMIT))

    try:
        _ensure_initialized(db_dir)
        conn = get_archive_connection(db_dir)
    except Exception:
        logger.warning("archive: 검색 연결 실패", exc_info=True)
        return []

    params: List[Any] = []
    scope_clause = ""
    if scope_key:
        scope_clause = " AND t.scope_key = ?"
    if project_key:
        # 태그는 저장을 가르지 않고 조회만 좁힌다.
        scope_clause += " AND t.project_key = ?"

    try:
        if len(text) >= _MIN_TRIGRAM_QUERY:
            # trigram 은 구문 검색만 지원한다. 사용자 입력을 FTS 연산자로
            # 해석하지 않도록 통째로 따옴표에 넣는다.
            match_expr = '"' + text.replace('"', '""') + '"'
            params = [match_expr]
            if scope_key:
                params.append(scope_key)
            if project_key:
                params.append(project_key)
            params.append(limit)
            sql = f"""
                SELECT t.id, t.session_id, t.scope_key, t.project_key, t.role, t.ts,
                       snippet(turns_fts, 0, '«', '»', ' … ', 24) AS content
                  FROM turns_fts
                  JOIN turns t ON t.id = turns_fts.rowid
                 WHERE turns_fts MATCH ?{scope_clause}
              ORDER BY t.id DESC
                 LIMIT ?
            """
        else:
            # 2자 이하 — trigram 인덱스로는 못 찾으므로 LIKE 로 내려간다.
            params = ["%" + text.replace("%", r"\%").replace("_", r"\_") + "%"]
            if scope_key:
                params.append(scope_key)
            if project_key:
                params.append(project_key)
            params.append(limit)
            sql = f"""
                SELECT t.id, t.session_id, t.scope_key, t.project_key, t.role, t.ts,
                       substr(t.content, 1, 200) AS content
                  FROM turns t
                 WHERE t.content LIKE ? ESCAPE '\\'{scope_clause}
              ORDER BY t.id DESC
                 LIMIT ?
            """
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_turn(r) for r in rows]
    except sqlite3.Error:
        logger.warning("archive: 검색 실패", exc_info=True)
        return []
    finally:
        conn.close()


def scroll_turns(
    turn_id: int,
    *,
    before: int = 3,
    after: int = 3,
    scope_key: Optional[str] = None,
    db_dir: "str | Path | None" = None,
) -> List[Dict[str, Any]]:
    """앵커 턴의 앞뒤 대화를 원문 그대로 돌려준다.

    검색 히트 한 줄만으로는 맥락을 못 읽는다. 우리 실측 평균 턴 길이가
    user 86B / assistant 309B 라서, 필요한 건 더 잘게 쪼개는 게 아니라
    앞뒤로 붙이는 것이다. 같은 세션 안에서만 확장한다.
    """
    before = max(0, min(int(before), _MAX_SCROLL_SPAN))
    after = max(0, min(int(after), _MAX_SCROLL_SPAN))
    try:
        _ensure_initialized(db_dir)
        conn = get_archive_connection(db_dir)
    except Exception:
        logger.warning("archive: scroll 연결 실패", exc_info=True)
        return []
    try:
        anchor_sql = "SELECT id, session_id, scope_key FROM turns WHERE id = ?"
        anchor_params: List[Any] = [int(turn_id)]
        if scope_key is not None:
            anchor_sql += " AND scope_key = ?"
            anchor_params.append(scope_key)
        anchor = conn.execute(anchor_sql, anchor_params).fetchone()
        if anchor is None:
            return []
        session_id = anchor["session_id"]
        # session_id 가 NULL 인 턴은 IS NOT DISTINCT FROM 이 필요하므로 분기한다.
        if session_id is None:
            scope = "t.session_id IS NULL"
            scope_params: List[Any] = []
        else:
            scope = "t.session_id = ?"
            scope_params = [session_id]
        if scope_key is not None:
            scope += " AND t.scope_key = ?"
            scope_params.append(scope_key)

        head = conn.execute(
            f"""SELECT t.id, t.session_id, t.scope_key, t.role, t.ts, t.content
                  FROM turns t WHERE {scope} AND t.id < ?
              ORDER BY t.id DESC LIMIT ?""",
            [*scope_params, int(turn_id), before],
        ).fetchall()
        tail = conn.execute(
            f"""SELECT t.id, t.session_id, t.scope_key, t.role, t.ts, t.content
                  FROM turns t WHERE {scope} AND t.id >= ?
              ORDER BY t.id ASC LIMIT ?""",
            [*scope_params, int(turn_id), after + 1],
        ).fetchall()
        return [_row_to_turn(r) for r in reversed(head)] + [_row_to_turn(r) for r in tail]
    except sqlite3.Error:
        logger.warning("archive: scroll 실패", exc_info=True)
        return []
    finally:
        conn.close()


def project_counts(
    *, scope_key: Optional[str] = None, db_dir: "str | Path | None" = None
) -> List[Dict[str, Any]]:
    """태그별 턴 수. 빈 태그는 미분류로 함께 돌려준다 — 얼마나 못 붙였는지가
    태그 품질의 지표라서, 숨기면 판정 실패가 안 보인다."""
    try:
        _ensure_initialized(db_dir)
        conn = get_archive_connection(db_dir)
    except Exception:
        logger.warning("archive: 태그 집계 연결 실패", exc_info=True)
        return []
    try:
        where, params = "", []
        if scope_key:
            where, params = " WHERE scope_key = ?", [scope_key]
        rows = conn.execute(
            f"SELECT project_key, COUNT(*) AS turns FROM turns{where}"
            " GROUP BY project_key ORDER BY turns DESC",
            params,
        ).fetchall()
        return [{"project_key": r["project_key"], "turns": r["turns"]} for r in rows]
    except sqlite3.Error:
        logger.warning("archive: 태그 집계 실패", exc_info=True)
        return []
    finally:
        conn.close()


def archive_stats(db_dir: "str | Path | None" = None) -> Dict[str, Any]:
    """적재량 확인용. 용량 추정을 단정하지 말고 이걸로 재라."""
    path = get_archive_path(db_dir)
    stats: Dict[str, Any] = {
        "path": str(path),
        "file_bytes": path.stat().st_size if path.exists() else 0,
        "turns": 0,
        "content_bytes": 0,
        "sessions": 0,
        "oldest_ts": None,
        "newest_ts": None,
    }
    if not path.exists():
        return stats
    try:
        conn = get_archive_connection(db_dir)
    except Exception:
        return stats
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(length(content)), 0) AS b,
                      COUNT(DISTINCT session_id) AS s,
                      MIN(ts) AS oldest, MAX(ts) AS newest
                 FROM turns"""
        ).fetchone()
        stats.update(
            turns=row["n"],
            content_bytes=row["b"],
            sessions=row["s"],
            oldest_ts=row["oldest"],
            newest_ts=row["newest"],
        )
    except sqlite3.Error:
        logger.warning("archive: 통계 조회 실패", exc_info=True)
    finally:
        conn.close()
    return stats
