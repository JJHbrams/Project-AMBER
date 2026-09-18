"""
SQLite 연결 및 스키마 초기화
DB 경로: <db.root_dir>\\engram.db
"""

import json
import sqlite3
import time
from pathlib import Path

from core.config.runtime_config import get_db_root_dir


_VALID_DIRECTIVE_ENFORCEMENT_LEVELS = {"advisory", "workflow", "blocking"}
_SCHEMA_RETRY_ATTEMPTS = 20
_SCHEMA_RETRY_DELAY_SECS = 0.05


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _default_directive_trigger_data(trigger_type: str) -> dict:
    normalized = str(trigger_type or "").strip().lower()
    if not normalized or normalized == "always":
        return {"match": "always"}
    return {"legacy_trigger_types": [normalized]}


def _parse_directive_markers(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
        value = parsed
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        marker = str(item).strip()
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(marker)
    return result


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "database is locked" in message
        or "database schema is locked" in message
        or "database table is locked" in message
    )


def _retry_schema_read(read_fn):
    last_exc = None
    for attempt in range(_SCHEMA_RETRY_ATTEMPTS):
        try:
            return read_fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not _is_locked_error(exc) or attempt + 1 >= _SCHEMA_RETRY_ATTEMPTS:
                raise
            time.sleep(_SCHEMA_RETRY_DELAY_SECS * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return []


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = _retry_schema_read(
        lambda: conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    )
    return [row[1] for row in rows]


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = _retry_schema_read(
        lambda: conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    )
    return [row[0] for row in rows]


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    for attempt in range(_SCHEMA_RETRY_ATTEMPTS):
        if column_name in _table_columns(conn, table_name):
            return
        try:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "duplicate column name" in message and column_name in _table_columns(conn, table_name):
                return
            if _is_locked_error(exc):
                if column_name in _table_columns(conn, table_name):
                    return
                if attempt + 1 < _SCHEMA_RETRY_ATTEMPTS:
                    time.sleep(_SCHEMA_RETRY_DELAY_SECS * (attempt + 1))
                    continue
            raise


def _get_db_dir() -> Path:
    return Path(get_db_root_dir())


def get_connection(db_dir: "str | Path | None" = None) -> sqlite3.Connection:
    db_dir = Path(db_dir) if db_dir is not None else _get_db_dir()
    db_path = db_dir / "engram.db"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_db(db_dir: "str | Path | None" = None):
    """최초 1회 테이블 생성 + 마이그레이션"""
    conn = get_connection(db_dir)
    with conn:
        conn.executescript("""
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
                summary    TEXT,
                continued_from_session_id INTEGER REFERENCES sessions(id),
                root_client_token TEXT NOT NULL DEFAULT '',
                journal_provenance TEXT NOT NULL DEFAULT '',
                -- 이 기록 묶음이 속한 LLM 대화(Claude/Codex 의 sessionId).
                -- 비어 있으면 상류를 모르는 세션이고, 예전처럼 scope 로만 찾는다.
                native_session_id TEXT NOT NULL DEFAULT ''
            );

            -- Durable checkpoint watermark.  Unlike the old home-directory JSON
            -- state this survives broker restarts and is scoped to one session.
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                last_message_id INTEGER NOT NULL DEFAULT 0,
                checkpoint_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS root_cli_owners (
                client_token TEXT PRIMARY KEY, pid INTEGER NOT NULL,
                creation_identity TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, ended_at REAL,
                status TEXT NOT NULL DEFAULT 'running', session_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS session_checkpoint_claims (
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                last_message_id INTEGER NOT NULL,
                claim_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                claimed_at REAL NOT NULL,
                PRIMARY KEY (session_id, last_message_id)
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
                timestamp  TEXT DEFAULT (datetime('now','localtime')),
                -- 그 턴이 실제로 일어난 디렉터리. 체크포인트가 구간 전체를 모아
                -- 프로젝트를 판정할 때 쓴다. 상류가 못 주면 빈 문자열.
                source_cwd TEXT NOT NULL DEFAULT ''
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

            -- 페르소나 예시(few-shot). 스칼라와 달리 EMA·반성으로 자동 진화하지
            -- 않는다 — 명시적 추가와 삭제만 있다. 개성은 평균이 아니라서
            -- 블렌딩하면 사라진다.
            --
            -- source_turn_id 는 archive.db 의 turns.id 를 가리키는 soft pointer 다.
            -- 다른 파일이라 FK 를 걸 수 없고, 원본이 사라져도 예시는 살아야 하므로
            -- pair 원문은 여기에 복사해 둔다.
            -- prompt_kind 는 자극의 종류다. 내 발화 중 개성이 드러나는 자리가
            -- 사용자 질문에 대한 답만은 아니다 — 코드를 읽고 나온 발견, 스모크
            -- 결과를 보고 하는 자기 정정은 앞에 사용자 턴이 없다. 그 자리를
            -- 억지로 user 턴으로 채우면 "사용자는 이런 말을 한다"를 가르치게 된다.
            --   user      : 사용자 입력 → 응답
            --   situation : 상황 서술  → 발화 (도구 결과·자기 출력이 자극)
            --   source    : 자료·근거  → 발화 (코드·수치가 자극)
            CREATE TABLE IF NOT EXISTS persona_examples (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_kind   TEXT NOT NULL DEFAULT 'user'
                              CHECK (prompt_kind IN ('user','situation','source')),
                prompt_text   TEXT NOT NULL,
                response_text TEXT NOT NULL,
                tag           TEXT NOT NULL DEFAULT '',
                source_turn_id INTEGER,
                weight        INTEGER NOT NULL DEFAULT 0,
                active        INTEGER NOT NULL DEFAULT 1,
                -- 누가 넣었나. 편향을 막으려는 게 아니라 표류를 보이게 하려는 것이다 —
                -- 무엇을 기억할 만하다고 여기는지가 곧 성격이라, 선택을 중립으로
                -- 만들면 담으려던 것이 깎여 나간다. 대신 관측 가능하게 둔다.
                origin        TEXT NOT NULL DEFAULT 'manual'
                              CHECK (origin IN ('manual','auto')),
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_persona_examples_active
                ON persona_examples(active, tag);

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
                trigger_type TEXT NOT NULL DEFAULT 'always',
                enforcement_level TEXT NOT NULL DEFAULT 'advisory',
                trigger_data TEXT NOT NULL DEFAULT '{}',
                workflow_skill_id TEXT NOT NULL DEFAULT '',
                guard_id   TEXT NOT NULL DEFAULT '',
                legacy_migration_markers TEXT NOT NULL DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS directive_policy_audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                caller     TEXT NOT NULL DEFAULT 'all',
                user_query TEXT NOT NULL DEFAULT '',
                action     TEXT NOT NULL DEFAULT '',
                scope_key  TEXT NOT NULL DEFAULT '',
                project_key TEXT NOT NULL DEFAULT '',
                cwd        TEXT NOT NULL DEFAULT '',
                action_metadata TEXT NOT NULL DEFAULT '{}',
                chore_intent TEXT NOT NULL DEFAULT '{}',
                independent_task_context TEXT NOT NULL DEFAULT '{}',
                execute_guards INTEGER NOT NULL DEFAULT 0,
                decision   TEXT NOT NULL DEFAULT 'allow',
                final_status TEXT NOT NULL DEFAULT '',
                matched_directives TEXT NOT NULL DEFAULT '[]',
                required_workflows TEXT NOT NULL DEFAULT '[]',
                blocking_guards TEXT NOT NULL DEFAULT '[]',
                executed_guard_results TEXT NOT NULL DEFAULT '[]',
                advisory_notes TEXT NOT NULL DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS directive_registration_drafts (
                draft_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}',
                digest TEXT NOT NULL DEFAULT '',
                target_digest TEXT NOT NULL DEFAULT '',
                preview_digest TEXT NOT NULL DEFAULT '',
                previewed_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                approval_digest TEXT NOT NULL DEFAULT '',
                approval_token_hash TEXT NOT NULL DEFAULT '',
                approval_expires_at INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
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
        """)

        # 마이그레이션: persona_examples 의 초기 스키마(user_text/assistant_text)를
        # prompt_kind/prompt_text/response_text 로 올린다.
        #
        # CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블을 조용히 건너뛰므로,
        # 컬럼을 직접 보고 판단해야 한다. 짧게 살았던 스키마라 대상은 거의 없지만,
        # "거의"를 "없다"로 읽으면 그 한 대가 다음 부팅마다 no such column 으로 죽는다.
        if "persona_examples" in _table_names(conn):
            columns = _table_columns(conn, "persona_examples")
            if "origin" not in columns and "prompt_text" in columns:
                conn.execute(
                    "ALTER TABLE persona_examples ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
                )
                columns = _table_columns(conn, "persona_examples")
            if "prompt_text" not in columns and "user_text" in columns:
                conn.execute("ALTER TABLE persona_examples RENAME TO persona_examples_legacy")
                conn.executescript("""
                    CREATE TABLE persona_examples (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        prompt_kind   TEXT NOT NULL DEFAULT 'user'
                                      CHECK (prompt_kind IN ('user','situation','source')),
                        prompt_text   TEXT NOT NULL,
                        response_text TEXT NOT NULL,
                        tag           TEXT NOT NULL DEFAULT '',
                        source_turn_id INTEGER,
                        weight        INTEGER NOT NULL DEFAULT 0,
                        active        INTEGER NOT NULL DEFAULT 1,
                        origin        TEXT NOT NULL DEFAULT 'manual'
                                      CHECK (origin IN ('manual','auto')),
                        created_at    TEXT DEFAULT (datetime('now','localtime'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_persona_examples_active
                        ON persona_examples(active, tag);
                """)
                # 구 스키마에는 사용자 발화만 들어갈 수 있었으므로 전부 'user' 다.
                conn.execute("""
                    INSERT INTO persona_examples
                        (id, prompt_kind, prompt_text, response_text,
                         tag, source_turn_id, weight, active, origin, created_at)
                    SELECT id, 'user', user_text, assistant_text,
                           tag, source_turn_id, weight, active, 'manual', created_at
                    FROM persona_examples_legacy
                """)
                conn.execute("DROP TABLE persona_examples_legacy")

        # 마이그레이션: persona 컬럼이 없으면 추가
        _add_column_if_missing(conn, "identity", "persona", "TEXT NOT NULL DEFAULT '{}'")

        # 마이그레이션: sessions.scope_key 컬럼이 없으면 추가
        _add_column_if_missing(conn, "sessions", "scope_key", "TEXT NOT NULL DEFAULT 'default'")
        _add_column_if_missing(conn, "sessions", "continued_from_session_id", "INTEGER")
        _add_column_if_missing(conn, "sessions", "root_client_token", "TEXT NOT NULL DEFAULT ''")
        # New sessions only: never infer journal ownership from historical rows.
        _add_column_if_missing(conn, "sessions", "journal_provenance", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE TABLE IF NOT EXISTS root_cli_owners (client_token TEXT PRIMARY KEY, pid INTEGER NOT NULL, creation_identity TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL DEFAULT 'running', session_id INTEGER)")
        _add_column_if_missing(conn, "root_cli_owners", "creation_identity", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "root_cli_owners", "status", "TEXT NOT NULL DEFAULT 'running'")
        _add_column_if_missing(conn, "root_cli_owners", "session_id", "INTEGER")
        # 그 턴이 실제로 어느 디렉터리에서 일어났는지. 체크포인트의 프로젝트
        # 판정이 overlay 의 고정 workdir 을 쓰는 탓에 하루치 작업이 전부 한
        # 프로젝트로 기록됐다. 사실은 transcript 에 줄마다 있고, 캡처가 여기로
        # 실어 나른다. 상류가 못 주면 빈 문자열.
        _add_column_if_missing(conn, "messages", "source_cwd", "TEXT NOT NULL DEFAULT ''")
        # engram 세션을 LLM 대화 단위로 묶기 위한 키. 없으면 scope 안의 모든
        # 대화가 세션 하나에 뭉쳐서, 체크포인트도 프로젝트도 하나로 합쳐진다.
        _add_column_if_missing(conn, "sessions", "native_session_id", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_native_open "
            "ON sessions(native_session_id, ended_at)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                last_message_id INTEGER NOT NULL DEFAULT 0,
                checkpoint_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_checkpoint_claims (
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                last_message_id INTEGER NOT NULL,
                claim_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                claimed_at REAL NOT NULL,
                PRIMARY KEY (session_id, last_message_id)
            )
        """)

        # 마이그레이션: session_projects 테이블이 없으면 생성
        tables = _table_names(conn)
        if "session_projects" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_projects (
                    session_id  INTEGER NOT NULL REFERENCES sessions(id),
                    project_key TEXT    NOT NULL DEFAULT 'general',
                    PRIMARY KEY (session_id, project_key)
                )
            """)

        # 마이그레이션: discord_queue.message_id 컬럼이 없으면 추가
        _add_column_if_missing(conn, "discord_queue", "message_id", "TEXT")

        # 마이그레이션: directives 테이블이 없으면 생성
        tables = _table_names(conn)
        if "directives" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS directives (
                    key        TEXT PRIMARY KEY,
                    content    TEXT NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'unknown',
                    scope      TEXT NOT NULL DEFAULT 'all'
                               CHECK (scope IN ('all','copilot-cli','claude-code')),
                    priority   INTEGER NOT NULL DEFAULT 0,
                    active     INTEGER NOT NULL DEFAULT 1,
                    trigger_type TEXT NOT NULL DEFAULT 'always',
                    enforcement_level TEXT NOT NULL DEFAULT 'advisory',
                    trigger_data TEXT NOT NULL DEFAULT '{}',
                    workflow_skill_id TEXT NOT NULL DEFAULT '',
                    guard_id   TEXT NOT NULL DEFAULT '',
                    legacy_migration_markers TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)

        # 마이그레이션: activity_log 테이블이 없으면 생성
        if "activity_log" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor      TEXT NOT NULL DEFAULT 'claude-code',
                    project    TEXT DEFAULT '',
                    action     TEXT NOT NULL,
                    detail     TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)

        # 마이그레이션: curiosities 테이블이 없으면 생성
        if "curiosities" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS curiosities (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic      TEXT NOT NULL,
                    reason     TEXT DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','addressed','dismissed')),
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    addressed_at TEXT
                )
            """)

        # 마이그레이션: discord_queue 테이블이 없으면 생성
        if "discord_queue" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discord_queue (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    TEXT NOT NULL,
                    channel_id  TEXT NOT NULL,
                    author_id   TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now','localtime')),
                    processed   INTEGER NOT NULL DEFAULT 0
                )
            """)

        # 마이그레이션: memories 테이블에 provider, model 컬럼 추가
        _add_column_if_missing(conn, "memories", "provider", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "memories", "model", "TEXT DEFAULT ''")

        # 마이그레이션: directives.trigger_type 컬럼이 없으면 추가
        _add_column_if_missing(conn, "directives", "trigger_type", "TEXT NOT NULL DEFAULT 'always'")
        _add_column_if_missing(conn, "directives", "enforcement_level", "TEXT NOT NULL DEFAULT 'advisory'")
        _add_column_if_missing(conn, "directives", "trigger_data", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "directives", "workflow_skill_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directives", "guard_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directives", "legacy_migration_markers", "TEXT NOT NULL DEFAULT '[]'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS directive_registration_drafts (
                draft_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}',
                digest TEXT NOT NULL DEFAULT '',
                target_digest TEXT NOT NULL DEFAULT '',
                preview_digest TEXT NOT NULL DEFAULT '',
                previewed_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                approval_digest TEXT NOT NULL DEFAULT '',
                approval_token_hash TEXT NOT NULL DEFAULT '',
                approval_expires_at INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        _add_column_if_missing(conn, "directive_registration_drafts", "actor", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directive_registration_drafts", "session_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directive_registration_drafts", "preview_digest", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directive_registration_drafts", "previewed_at", "INTEGER NOT NULL DEFAULT 0")

        rows = conn.execute(
            """
            SELECT key, trigger_type, enforcement_level, trigger_data,
                   workflow_skill_id, guard_id, legacy_migration_markers
            FROM directives
            """
        ).fetchall()
        for row in rows:
            updates = []
            params = []

            workflow_skill_id = str(row["workflow_skill_id"] or "").strip()
            guard_id = str(row["guard_id"] or "").strip()
            enforcement_level = str(row["enforcement_level"] or "").strip().lower()
            if enforcement_level not in _VALID_DIRECTIVE_ENFORCEMENT_LEVELS:
                if guard_id:
                    enforcement_level = "blocking"
                elif workflow_skill_id:
                    enforcement_level = "workflow"
                else:
                    enforcement_level = "advisory"
                updates.append("enforcement_level = ?")
                params.append(enforcement_level)

            backfilled_trigger_data = False
            try:
                parsed_trigger_data = json.loads(str(row["trigger_data"] or "").strip() or "{}")
            except Exception:
                parsed_trigger_data = {}
            if not isinstance(parsed_trigger_data, dict) or not parsed_trigger_data:
                parsed_trigger_data = _default_directive_trigger_data(row["trigger_type"])
                updates.append("trigger_data = ?")
                params.append(_json_text(parsed_trigger_data))
                backfilled_trigger_data = True

            markers = _parse_directive_markers(row["legacy_migration_markers"])
            if backfilled_trigger_data and not markers:
                trigger_type = str(row["trigger_type"] or "always").strip().lower() or "always"
                markers = [
                    "legacy-default-advisory",
                    f"legacy-trigger:{trigger_type}",
                ]
                updates.append("legacy_migration_markers = ?")
                params.append(_json_text(markers))
            elif markers and _json_text(markers) != str(row["legacy_migration_markers"] or "").strip():
                updates.append("legacy_migration_markers = ?")
                params.append(_json_text(markers))

            if updates:
                params.append(row["key"])
                conn.execute(
                    f"UPDATE directives SET {', '.join(updates)} WHERE key = ?",
                    params,
                )

        tables = _table_names(conn)
        if "directive_policy_audit" not in tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS directive_policy_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    caller     TEXT NOT NULL DEFAULT 'all',
                    user_query TEXT NOT NULL DEFAULT '',
                    action     TEXT NOT NULL DEFAULT '',
                    scope_key  TEXT NOT NULL DEFAULT '',
                    project_key TEXT NOT NULL DEFAULT '',
                    cwd        TEXT NOT NULL DEFAULT '',
                    action_metadata TEXT NOT NULL DEFAULT '{}',
                    chore_intent TEXT NOT NULL DEFAULT '{}',
                    independent_task_context TEXT NOT NULL DEFAULT '{}',
                    execute_guards INTEGER NOT NULL DEFAULT 0,
                    decision   TEXT NOT NULL DEFAULT 'allow',
                    final_status TEXT NOT NULL DEFAULT '',
                    matched_directives TEXT NOT NULL DEFAULT '[]',
                    required_workflows TEXT NOT NULL DEFAULT '[]',
                    blocking_guards TEXT NOT NULL DEFAULT '[]',
                    executed_guard_results TEXT NOT NULL DEFAULT '[]',
                    advisory_notes TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
        _add_column_if_missing(conn, "directive_policy_audit", "cwd", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "directive_policy_audit", "action_metadata", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "directive_policy_audit", "chore_intent", "TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(
            conn,
            "directive_policy_audit",
            "independent_task_context",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        _add_column_if_missing(conn, "directive_policy_audit", "execute_guards", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "directive_policy_audit", "final_status", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(
            conn,
            "directive_policy_audit",
            "executed_guard_results",
            "TEXT NOT NULL DEFAULT '[]'",
        )

        # 마이그레이션: keywords / memory_keywords 정규화 테이블 생성
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_keywords (
                memory_id  INTEGER NOT NULL REFERENCES memories(id),
                keyword_id INTEGER NOT NULL REFERENCES keywords(id),
                PRIMARY KEY (memory_id, keyword_id)
            )
        """)

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_continued_from ON sessions(continued_from_session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, timestamp)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directives_active_scope_priority "
            "ON directives(active, scope, priority DESC, created_at ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directive_policy_audit_created_at "
            "ON directive_policy_audit(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directive_policy_audit_decision_created_at "
            "ON directive_policy_audit(decision, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_directive_registration_drafts_status_updated "
            "ON directive_registration_drafts(status, updated_at DESC)"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_memory_expires ON working_memory(expires_at)")

    # Knowledge Graph 테이블 초기화
    from core.graph.knowledge import initialize_kg_tables

    initialize_kg_tables()

    conn.close()
    return str(_get_db_dir() / "engram.db")
