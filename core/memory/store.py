"""
에피소드 기억 저장 및 검색 + 스코프 기반 단기/임시 메모리 유틸.
"""

import re
import threading
import concurrent.futures
from datetime import datetime, timezone
from typing import List, Optional, Dict

from core.storage.db import get_connection
from core.common.sanitizer import sanitize
from core.config.runtime_config import get_cfg_value, get_default_fallback_scope_key


DEFAULT_SCOPE_KEY = get_default_fallback_scope_key()
DEFAULT_PROJECT_KEY = "general"

# ThreadPoolExecutor for async EpisodeNode embedding (singleton)
# _embed_semaphore: 동시 큐 깊이를 50으로 제한 — 초과 시 task drop (OOM 방지)
_embed_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="engram_embed")
_embed_semaphore = threading.BoundedSemaphore(50)


def _submit_embed_task(*args) -> None:
    """임베딩 task를 executor에 submit. 큐가 꽉 찼으면 drop."""
    if not _embed_semaphore.acquire(blocking=False):
        return
    def _wrapped():
        try:
            _async_upsert_episode(*args)
        finally:
            _embed_semaphore.release()
    _embed_executor.submit(_wrapped)


def _normalize_project_keys(keys: Optional[List[str]]) -> List[str]:
    """project_keys 목록을 정규화한다. 비어있으면 ['general'] 반환."""
    if not keys:
        return [DEFAULT_PROJECT_KEY]
    safe = [sanitize(k, max_length=100).strip() for k in keys]
    safe = [k for k in safe if k]
    return safe if safe else [DEFAULT_PROJECT_KEY]


def create_session(scope_key: Optional[str] = None, project_keys: Optional[List[str]] = None) -> int:
    """스코프와 연관 프로젝트를 지정해 세션을 생성한다.
    project_keys가 비어있으면 'general'로 기록된다."""
    normalized_scope = _normalize_scope_key(scope_key)
    norm_keys = _normalize_project_keys(project_keys)
    conn = get_connection()
    cursor = conn.execute("INSERT INTO sessions (scope_key) VALUES (?)", (normalized_scope,))
    session_id = cursor.lastrowid
    for key in norm_keys:
        conn.execute(
            "INSERT OR IGNORE INTO session_projects (session_id, project_key) VALUES (?, ?)",
            (session_id, key),
        )
    conn.commit()
    conn.close()
    return int(session_id)


def close_session(session_id: int, summary: str = "") -> None:
    """세션 종료 시각과 요약을 sessions 테이블에 기록한다."""
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE sessions SET ended_at=datetime('now','localtime'), summary=? WHERE id=?",
            (sanitize(summary, max_length=500) if summary else None, session_id),
        )
    conn.close()


def link_session_projects(session_id: int, project_keys: List[str]) -> None:
    """기존 세션에 프로젝트 연관을 추가한다 (중복은 무시)."""
    norm_keys = _normalize_project_keys(project_keys)
    conn = get_connection()
    with conn:
        for key in norm_keys:
            conn.execute(
                "INSERT OR IGNORE INTO session_projects (session_id, project_key) VALUES (?, ?)",
                (session_id, key),
            )
    conn.close()


def get_session_projects(session_id: int) -> List[str]:
    """세션에 연관된 프로젝트 키 목록을 반환한다."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT project_key FROM session_projects WHERE session_id = ? ORDER BY project_key",
        (session_id,),
    ).fetchall()
    conn.close()
    return [r["project_key"] for r in rows]


def save_message(session_id: int, role: str, content: str):
    safe_content = sanitize(content, max_length=4000)
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)", (session_id, role, safe_content))
    conn.close()


def resolve_session_id_by_scope(scope_key: Optional[str]) -> Optional[int]:
    """스코프에 해당하는 가장 최근 세션 ID를 반환한다."""
    normalized = _normalize_scope_key(scope_key)
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM sessions WHERE scope_key = ? ORDER BY started_at DESC LIMIT 1",
        (normalized,),
    ).fetchone()
    conn.close()
    return int(row["id"]) if row else None


def _format_memory(content: str, source: str, provider: str, project: str = "") -> str:
    """LTM 저장용 마크다운 포맷으로 변환.
    source: 'save' | 'close'
    이미 포맷이 적용된 내용이면 그대로 반환."""
    if content.startswith("---\nsource:"):
        return content
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    meta_parts = [f"source: {source}"]
    if provider:
        meta_parts.append(f"provider: {provider}")
    if project:
        meta_parts.append(f"project: {project}")
    header = "---\n" + "\n".join(meta_parts) + f"\ndate: {date_str}\n---\n\n"
    return header + content


def save_memory(
    session_id: Optional[int],
    content: str,
    keywords: Optional[str] = None,
    provider: str = "",
    model: str = "",
    source: str = "save",
    project: str = "",
):
    # sanitize 먼저(인젝션 방어), 이후 frontmatter 래핑 (--- 가 sanitize에서 ⟦---⟧ 로 깨지는 것 방지)
    safe_body = sanitize(content, max_length=2900)
    safe_content = _format_memory(safe_body, source, provider, project)
    if keywords is None:
        keywords = _extract_keywords(safe_content)
    
    conn = get_connection()
    with conn:
        # 1. memories 테이블 저장 (기존 필드 유지)
        cursor = conn.execute(
            "INSERT INTO memories (session_id, content, keywords, provider, model) VALUES (?,?,?,?,?)",
            (session_id, safe_content, keywords, provider, model),
        )
        episode_id = cursor.lastrowid
        
        # 2. 정규화된 키워드 테이블 저장
        if keywords.strip():
            # 쉼표나 공백으로 분리
            words = set()
            for part in keywords.replace(",", " ").split():
                w = part.strip().lower()
                if len(w) > 1:
                    words.add(w)
            
            for w in words:
                # 키워드 원본 저장 (중복 무시)
                conn.execute("INSERT OR IGNORE INTO keywords (name) VALUES (?)", (w,))
                row = conn.execute("SELECT id FROM keywords WHERE name = ?", (w,)).fetchone()
                if row:
                    kw_id = row["id"]
                    # 메모리-키워드 매핑 저장
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_keywords (memory_id, keyword_id) VALUES (?, ?)",
                        (episode_id, kw_id)
                    )
    conn.close()
    
    # EpisodeNode 비동기 임베딩 (ThreadPoolExecutor, bounded queue)
    _submit_embed_task(str(episode_id), safe_content, keywords, str(session_id or ""), provider, model)


def _async_upsert_episode(episode_id: str, content: str, keywords: str, session_id: str, provider: str = "", model: str = ""):
    """백그라운드 스레드에서 EpisodeNode를 KuzuDB에 upsert한다."""
    try:
        # core.graph.semantic 패키지는 stm_promoter ↔ store 순환이 있어 지연 import 유지.
        from core.graph.semantic import get_semantic_graph

        sg = get_semantic_graph()
        if sg.enabled:
            sg.upsert_episode(
                episode_id=episode_id,
                content=content,
                keywords=keywords,
                session_id=session_id,
                created_at=datetime.now(tz=timezone.utc).isoformat(),
            )
    except Exception:
        pass


def search_memories(query: str, limit: int = 5, max_age_days: int = 0) -> List[str]:
    """EpisodeNode 시맨틱 검색 (SemanticGraph 활성 시 우선), 아니면 키워드 겹침 기반 fallback.

    Args:
        query: 검색어
        limit: 반환할 최대 결과 수
        max_age_days: 0이면 전체 기간, 양수면 최근 N일 이내만 검색
    """
    # 1. Semantic search via EpisodeNode (primary)
    if query:
        try:
            # core.graph.semantic 패키지는 stm_promoter ↔ store 순환이 있어 지연 import 유지.
            from core.graph.semantic import get_semantic_graph
            sg = get_semantic_graph()
            if sg.enabled:
                hits = sg.episode_semantic_search(
                    query, top_k=limit, threshold=0.25, max_age_days=max_age_days
                )
                if hits:
                    return [h["content"] for h in hits]
        except Exception:
            pass

    # 2. Keyword fallback (SQLite)
    query_words = set(_tokenize(query))
    conn = get_connection()
    rows = conn.execute("SELECT content, keywords FROM memories ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()

    if not query_words:
        return [row["content"] for row in rows[:limit]]

    scored = []
    for row in rows:
        kw = set(_tokenize(row["keywords"] or ""))
        content_words = set(_tokenize(row["content"]))
        overlap = len(query_words & (kw | content_words))
        if overlap > 0:
            scored.append((overlap, row["content"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def get_recent_messages(session_id: int, limit: int = 20) -> List[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT role, content FROM messages
           WHERE session_id = ?
           ORDER BY timestamp ASC LIMIT ?""",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_recent_messages_by_scope(
    scope_key: Optional[str],
    limit: Optional[int] = None,
    within_minutes: Optional[int] = None,
) -> List[dict]:
    """스코프 내 최근 메시지를 세션 경계를 넘어 조회한다."""
    normalized_scope = _normalize_scope_key(scope_key)
    default_limit = int(get_cfg_value("memory.short_term.limit_turns", 8))
    default_window = int(get_cfg_value("memory.short_term.within_minutes", 120))
    safe_limit = max(1, min(limit if limit is not None else default_limit, 50))
    safe_window = max(1, min(within_minutes if within_minutes is not None else default_window, 60 * 24 * 7))

    conn = get_connection()
    rows = conn.execute(
        """SELECT m.role, m.content
           FROM messages m
           JOIN sessions s ON s.id = m.session_id
           WHERE s.scope_key = ?
             AND m.timestamp >= datetime('now','localtime', ?)
           ORDER BY m.timestamp DESC
           LIMIT ?""",
        (normalized_scope, f"-{safe_window} minutes", safe_limit),
    ).fetchall()
    conn.close()

    # 최신순으로 가져왔으므로 시간 오름차순으로 재정렬
    rows = list(reversed(rows))
    # context 주입 시 토큰 절약: 500자로 truncate (전문은 DB에 보존)
    return [{"role": r["role"], "content": r["content"][:500]} for r in rows]


def get_working_memory(scope_key: str) -> Dict[str, str]:
    """스코프의 임시 요약 메모리를 조회한다(만료 시 자동 무시)."""
    normalized_scope = _normalize_scope_key(scope_key)
    conn = get_connection()
    row = conn.execute(
        """SELECT summary, open_intents, updated_at, expires_at
           FROM working_memory
           WHERE scope_key = ?
             AND (expires_at IS NULL OR expires_at > datetime('now','localtime'))""",
        (normalized_scope,),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "summary": row["summary"] or "",
        "open_intents": row["open_intents"] or "",
        "updated_at": row["updated_at"] or "",
        "expires_at": row["expires_at"] or "",
    }


def upsert_working_memory(
    scope_key: Optional[str],
    summary: str,
    open_intents: str = "",
    ttl_hours: Optional[int] = None,
):
    """임시 메모리를 upsert하며 만료시간을 갱신한다."""
    normalized_scope = _normalize_scope_key(scope_key)
    summary_max = int(get_cfg_value("memory.working.store_summary_max_chars", 1200))
    intents_max = int(get_cfg_value("memory.working.store_open_intents_max_chars", 600))
    default_ttl = int(get_cfg_value("memory.working.ttl_hours", 48))

    safe_summary = sanitize(summary, max_length=summary_max)
    safe_open_intents = sanitize(open_intents, max_length=intents_max)
    ttl_value = ttl_hours if ttl_hours is not None else default_ttl
    safe_ttl = max(1, min(ttl_value, 24 * 30))

    conn = get_connection()
    with conn:
        conn.execute(
            """INSERT INTO working_memory (scope_key, summary, open_intents, updated_at, expires_at)
               VALUES (?, ?, ?, datetime('now','localtime'), datetime('now','localtime', ?))
               ON CONFLICT(scope_key) DO UPDATE SET
                 summary = excluded.summary,
                 open_intents = excluded.open_intents,
                 updated_at = datetime('now','localtime'),
                 expires_at = excluded.expires_at""",
            (normalized_scope, safe_summary, safe_open_intents, f"+{safe_ttl} hours"),
        )
        conn.execute("DELETE FROM working_memory WHERE expires_at IS NOT NULL AND expires_at <= datetime('now','localtime')")
    conn.close()


def append_working_memory_hint(
    scope_key: Optional[str],
    user_content: str,
    assistant_content: str,
    ttl_hours: Optional[int] = None,
    max_length: Optional[int] = None,
):
    """최근 턴을 임시 메모리 요약 문자열에 누적한다."""
    default_ttl = int(get_cfg_value("memory.working.ttl_hours", 48))
    default_max_length = int(get_cfg_value("memory.working.max_compact_length", 900))
    user_clip = int(get_cfg_value("memory.working.user_clip_chars", 120))
    assistant_clip = int(get_cfg_value("memory.working.assistant_clip_chars", 160))

    ttl_value = ttl_hours if ttl_hours is not None else default_ttl
    max_length_value = max_length if max_length is not None else default_max_length

    current = get_working_memory(scope_key)
    existing = current.get("summary", "")

    turn = f"U:{_clip(user_content, user_clip)} | A:{_clip(assistant_content, assistant_clip)}"
    merged = turn if not existing else f"{existing} || {turn}"
    if len(merged) > max_length_value:
        merged = merged[-max_length_value:]

    upsert_working_memory(scope_key, merged, open_intents=current.get("open_intents", ""), ttl_hours=ttl_value)


def list_memories(limit: int = 20) -> List[str]:
    conn = get_connection()
    rows = conn.execute("SELECT content, created_at FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [f"[{r['created_at']}] {r['content']}" for r in rows]


def _extract_keywords(text: str) -> str:
    words = _tokenize(text)
    # 2글자 이상, 불용어 제외
    stop = {"이", "그", "저", "것", "수", "등", "및", "는", "을", "를", "이다", "있다"}
    return " ".join(w for w in set(words) if len(w) >= 2 and w not in stop)


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[가-힣a-zA-Z0-9]+", (text or "").lower())


def _normalize_scope_key(scope_key: Optional[str]) -> str:
    key = (scope_key or "").strip()
    if key:
        return key
    return str(get_cfg_value("memory.scope.default_fallback", get_default_fallback_scope_key()))


def _clip(text: str, limit: int) -> str:
    cleaned = sanitize(text or "", max_length=limit)
    return cleaned


