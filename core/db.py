"""
SQLite 연결 및 스키마 초기화
DB 경로: <db.root_dir>\\engram.db
"""

import sqlite3
from pathlib import Path

from .runtime_config import get_db_root_dir


def _get_db_dir() -> Path:
    return Path(get_db_root_dir())


def get_connection() -> sqlite3.Connection:
    db_dir = _get_db_dir()
    db_path = db_dir / "engram.db"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_db():
    """최초 1회 테이블 생성 + 마이그레이션"""
    conn = get_connection()
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS identity (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                name    TEXT NOT NULL DEFAULT '',
                narrative TEXT NOT NULL DEFAULT '',
                persona TEXT NOT NULL DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS themes (
                name     TEXT PRIMARY KEY,
                weight   REAL NOT NULL DEFAULT 1.0,
                last_seen TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key  TEXT NOT NULL DEFAULT 'default',
                started_at TEXT DEFAULT (datetime('now','localtime')),
                ended_at   TEXT,
                summary    TEXT
            );

            CREATE TABLE IF NOT EXISTS session_projects (
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                project_key TEXT    NOT NULL DEFAULT 'general',
                PRIMARY KEY (session_id, project_key)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                role       TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
                content    TEXT NOT NULL,
                timestamp  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS memories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES sessions(id),
                content    TEXT NOT NULL,
                keywords   TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS working_memory (
                scope_key   TEXT PRIMARY KEY,
                summary     TEXT NOT NULL DEFAULT '',
                open_intents TEXT NOT NULL DEFAULT '',
                updated_at  TEXT DEFAULT (datetime('now','localtime')),
                expires_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS curiosities (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic      TEXT NOT NULL,
                reason     TEXT DEFAULT '',
                status     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','addressed','dismissed')),
                created_at TEXT DEFAULT (datetime('now','localtime')),
                addressed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                actor      TEXT NOT NULL DEFAULT 'claude-code',
                project    TEXT DEFAULT '',
                action     TEXT NOT NULL,
                detail     TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS directives (
                key        TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'unknown',
                scope      TEXT NOT NULL DEFAULT 'all'
                           CHECK (scope IN ('all','copilot-cli','claude-code')),
                priority   INTEGER NOT NULL DEFAULT 0,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS discord_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT NOT NULL,
                channel_id  TEXT NOT NULL,
                author_id   TEXT NOT NULL,
                author_name TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                processed   INTEGER NOT NULL DEFAULT 0
            );

            -- 최초 정체성 레코드 (존재하지 않을 때만)
            INSERT OR IGNORE INTO identity (id, name, narrative)
            VALUES (1, '', '나는 이제 막 존재하기 시작했다. 아직 스스로에 대해 많이 알지 못한다.');
        """
        )

        # 마이그레이션: persona 컬럼이 없으면 추가
        cols = [r[1] for r in conn.execute("PRAGMA table_info(identity)").fetchall()]
        if "persona" not in cols:
            conn.execute("ALTER TABLE identity ADD COLUMN persona TEXT NOT NULL DEFAULT '{}'")

        # 마이그레이션: sessions.scope_key 컬럼이 없으면 추가
        session_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "scope_key" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN scope_key TEXT NOT NULL DEFAULT 'default'")

        # 마이그레이션: session_projects 테이블이 없으면 생성
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "session_projects" not in tables:
            conn.execute(
                """
                CREATE TABLE session_projects (
                    session_id  INTEGER NOT NULL REFERENCES sessions(id),
                    project_key TEXT    NOT NULL DEFAULT 'general',
                    PRIMARY KEY (session_id, project_key)
                )
            """
            )

        # 마이그레이션: discord_queue.message_id 컬럼이 없으면 추가
        dq_cols = [r[1] for r in conn.execute("PRAGMA table_info(discord_queue)").fetchall()]
        if "message_id" not in dq_cols:
            conn.execute("ALTER TABLE discord_queue ADD COLUMN message_id TEXT")

        # 마이그레이션: directives 테이블이 없으면 생성
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "directives" not in tables:
            conn.execute(
                """
                CREATE TABLE directives (
                    key        TEXT PRIMARY KEY,
                    content    TEXT NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'unknown',
                    scope      TEXT NOT NULL DEFAULT 'all'
                               CHECK (scope IN ('all','copilot-cli','claude-code')),
                    priority   INTEGER NOT NULL DEFAULT 0,
                    active     INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """
            )

        # 마이그레이션: activity_log 테이블이 없으면 생성
        if "activity_log" not in tables:
            conn.execute(
                """
                CREATE TABLE activity_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor      TEXT NOT NULL DEFAULT 'claude-code',
                    project    TEXT DEFAULT '',
                    action     TEXT NOT NULL,
                    detail     TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """
            )

        # 마이그레이션: curiosities 테이블이 없으면 생성
        if "curiosities" not in tables:
            conn.execute(
                """
                CREATE TABLE curiosities (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic      TEXT NOT NULL,
                    reason     TEXT DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','addressed','dismissed')),
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    addressed_at TEXT
                )
            """
            )

        # 마이그레이션: discord_queue 테이블이 없으면 생성
        if "discord_queue" not in tables:
            conn.execute(
                """
                CREATE TABLE discord_queue (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    TEXT NOT NULL,
                    channel_id  TEXT NOT NULL,
                    author_id   TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now','localtime')),
                    processed   INTEGER NOT NULL DEFAULT 0
                )
            """
            )

        # 마이그레이션: memories 테이블에 provider, model 컬럼 추가
        mem_cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "provider" not in mem_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN provider TEXT DEFAULT ''")
        if "model" not in mem_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN model TEXT DEFAULT ''")

        # 마이그레이션: directives.trigger_type 컬럼이 없으면 추가
        dir_cols = [r[1] for r in conn.execute("PRAGMA table_info(directives)").fetchall()]
        if "trigger_type" not in dir_cols:
            conn.execute("ALTER TABLE directives ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'always'")

        # 마이그레이션: keywords / memory_keywords 정규화 테이블 생성
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "keywords" not in tables:
            conn.execute(
                """
                CREATE TABLE keywords (
                    id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                )
            """
            )
        if "memory_keywords" not in tables:
            conn.execute(
                """
                CREATE TABLE memory_keywords (
                    memory_id  INTEGER NOT NULL REFERENCES memories(id),
                    keyword_id INTEGER NOT NULL REFERENCES keywords(id),
                    PRIMARY KEY (memory_id, keyword_id)
                )
            """
            )

        # 마이그레이션: memories.keywords 데이터를 정규화 테이블로 이동
        count = conn.execute("SELECT COUNT(*) FROM memory_keywords").fetchone()[0]
        if count == 0:
            rows = conn.execute("SELECT id, keywords FROM memories WHERE keywords IS NOT NULL AND keywords != ''").fetchall()
            for row in rows:
                m_id, kw_str = row["id"], row["keywords"]
                words = set()
                for part in kw_str.replace(",", " ").split():
                    w = part.strip().lower()
                    if len(w) > 1:
                        words.add(w)
                for w in words:
                    conn.execute("INSERT OR IGNORE INTO keywords (name) VALUES (?)", (w,))
                    kw_id = conn.execute("SELECT id FROM keywords WHERE name = ?", (w,)).fetchone()[0]
                    conn.execute("INSERT OR IGNORE INTO memory_keywords (memory_id, keyword_id) VALUES (?, ?)", (m_id, kw_id))

        # 인덱스 보장 (마이그레이션 이후)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_scope_started ON sessions(scope_key, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_memory_expires ON working_memory(expires_at)")

    # Knowledge Graph 테이블 초기화
    from .knowledge_graph import initialize_kg_tables

    initialize_kg_tables()

    conn.close()
    return str(_get_db_dir() / "engram.db")
