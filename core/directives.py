"""
지침(Directives) 관리 모듈
세션 간 유지되는 운영 규칙을 저장하고 컨텍스트에 자동 주입한다.

source: 지침을 생성한 도구 ('copilot-cli', 'claude-code', 'user')
scope:  지침이 적용되는 대상 ('all', 'copilot-cli', 'claude-code')
trigger_type: 주입 조건 ('always' | 'wiki' | 'code' | 'git' | 'reflection')
"""

from typing import List, Dict, Optional
from core.db import get_connection
from core.sanitizer import sanitize


# ── 트리거 키워드 매핑 ────────────────────────────────────────────────────────
_TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "wiki": [
        "wiki", "문서", "노트", "작성", "기록", "저장", "vault",
        "kg_add", "kg_update", "kg_read", "위키", "정리",
    ],
    "code": [
        "코드", "수정", "구현", "버그", "디버깅", "리팩토링", "파일",
        "함수", "클래스", "모듈", "import", "fix", "refactor", "구현",
        "작성", "빌드", "테스트",
    ],
    "git": [
        "git", "커밋", "commit", "브랜치", "branch", "push", "merge",
        "pr", "풀리퀘", "rebase", "checkout",
    ],
    "reflection": [
        "reflect", "/reflect", "반성", "세션", "close_session",
        "피드백", "종료", "정리", "끝", "수고",
    ],
}


def _active_triggers(user_query: str) -> set[str]:
    """user_query에서 활성화된 trigger_type 집합을 반환한다."""
    if not user_query:
        return set()
    q = user_query.lower()
    active = set()
    for trigger, keywords in _TRIGGER_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            active.add(trigger)
    return active


def add_directive(
    key: str,
    content: str,
    source: str = "unknown",
    scope: str = "all",
    priority: int = 0,
    trigger_type: str = "always",
) -> dict:
    content = sanitize(content, max_length=1500)
    conn = get_connection()
    with conn:
        conn.execute(
            """INSERT INTO directives (key, content, source, scope, priority, trigger_type)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   content      = excluded.content,
                   source       = excluded.source,
                   scope        = excluded.scope,
                   priority     = excluded.priority,
                   trigger_type = excluded.trigger_type,
                   active       = 1,
                   updated_at   = datetime('now','localtime')""",
            (key, content, source, scope, priority, trigger_type),
        )
    conn.close()
    return {"key": key, "content": content, "source": source, "scope": scope, "trigger_type": trigger_type}


def get_directives(
    scope_filter: str = "all",
    include_inactive: bool = False,
    user_query: str = "",
) -> List[Dict]:
    """지침 목록 조회.

    - scope_filter: 대상 필터 ('all', 'copilot-cli', 'claude-code')
    - include_inactive: 비활성 지침 포함 여부
    - user_query: 트리거 필터링용 쿼리. 비어 있으면 trigger_type='always' 인 것만 반환.
      쿼리가 있으면 'always' + 활성화된 trigger 유형 모두 포함.
    """
    conn = get_connection()
    if include_inactive:
        rows = conn.execute(
            """SELECT key, content, source, scope, priority, active, trigger_type, created_at, updated_at
               FROM directives
               ORDER BY priority DESC, created_at ASC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # active + scope 필터
    rows = conn.execute(
        """SELECT key, content, source, scope, priority, active, trigger_type, created_at, updated_at
           FROM directives
           WHERE active = 1 AND (scope = 'all' OR scope = ?)
           ORDER BY priority DESC, created_at ASC""",
        (scope_filter,),
    ).fetchall()
    conn.close()

    directives = [dict(r) for r in rows]

    # trigger 필터
    active_triggers = _active_triggers(user_query)
    result = []
    for d in directives:
        t = d.get("trigger_type", "always")
        if t == "always" or t in active_triggers:
            result.append(d)

    return result


def update_directive(
    key: str,
    content: Optional[str] = None,
    scope: Optional[str] = None,
    priority: Optional[int] = None,
    active: Optional[bool] = None,
    trigger_type: Optional[str] = None,
) -> bool:
    """지침 수정. 전달된 필드만 업데이트."""
    updates = []
    params = []
    if content is not None:
        updates.append("content = ?")
        params.append(content)
    if scope is not None:
        updates.append("scope = ?")
        params.append(scope)
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if active is not None:
        updates.append("active = ?")
        params.append(1 if active else 0)
    if trigger_type is not None:
        updates.append("trigger_type = ?")
        params.append(trigger_type)
    if not updates:
        return False
    updates.append("updated_at = datetime('now','localtime')")
    params.append(key)

    conn = get_connection()
    with conn:
        cursor = conn.execute(
            f"UPDATE directives SET {', '.join(updates)} WHERE key = ?", params
        )
    conn.close()
    return cursor.rowcount > 0


def remove_directive(key: str) -> bool:
    """지침 완전 삭제."""
    conn = get_connection()
    with conn:
        cursor = conn.execute("DELETE FROM directives WHERE key = ?", (key,))
    conn.close()
    return cursor.rowcount > 0


def render_directives_prompt(caller: str = "all", user_query: str = "") -> str:
    """컨텍스트 주입용 지침 문자열 렌더링."""
    directives = get_directives(scope_filter=caller, user_query=user_query)
    if not directives:
        return ""
    lines = []
    for d in directives:
        scope_tag = f" [{d['scope']}]" if d["scope"] != "all" else ""
        lines.append(f"• {d['content']}{scope_tag}")
    return "[지침]\n" + "\n".join(lines)
