"""
활동 로그 — engram 외부 객체(Claude Code 등)가 수행한 작업을 3인칭으로 기록.
engram의 기억(memories)과는 별개이며, engram가 반성 시 참조하는 '보고서' 역할.
"""

from typing import List, Dict, Optional
from core.storage.db import get_connection


def log_activity(
    action: str,
    detail: str = "",
    project: str = "",
    actor: str = "claude-code",
) -> int:
    """활동을 기록하고 ID를 반환."""
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            """INSERT INTO activity_log (actor, project, action, detail)
               VALUES (?, ?, ?, ?)""",
            (actor, project, action, detail),
        )
        log_id = cursor.lastrowid
    conn.close()
    return log_id


def get_recent_activities(
    limit: int = 10,
    since_session_id: Optional[int] = None,
) -> List[Dict]:
    """최근 활동 로그를 조회. since_session_id가 주어지면 해당 세션 시작 이후 로그만."""
    conn = get_connection()

    if since_session_id:
        rows = conn.execute(
            """SELECT al.* FROM activity_log al
               JOIN sessions s ON s.id = ?
               WHERE al.created_at >= s.started_at
               ORDER BY al.created_at DESC LIMIT ?""",
            (since_session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def render_activity_for_reflection(since_session_id: Optional[int] = None, limit: int = 20) -> str:
    """반성 컨텍스트용 활동 요약 문자열 생성."""
    activities = get_recent_activities(limit=limit, since_session_id=since_session_id)
    if not activities:
        return ""

    lines = ["[외부 활동 로그 — engram 부재 중 발생한 작업]"]
    for a in reversed(activities):  # 시간순 정렬
        project_tag = f" [{a['project']}]" if a.get("project") else ""
        lines.append(f"- ({a['created_at']}) {a['actor']}{project_tag}: {a['action']}")
        if a.get("detail"):
            lines.append(f"  └ {a['detail'][:200]}")

    return "\n".join(lines)

