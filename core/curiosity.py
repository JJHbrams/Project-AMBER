"""
궁금증(curiosity) 큐 관리
반성 시 생성 → 다음 세션 시작 시 context에 주입 → 대화 후 addressed 처리
"""

from typing import Dict, List, Optional
from .db import get_connection
from .sanitizer import sanitize


def add_curiosity(topic: str, reason: str = "") -> int:
    """새 궁금증 추가. 생성된 id 반환."""
    topic = sanitize(topic, max_length=500)
    reason = sanitize(reason, max_length=500)
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            "INSERT INTO curiosities (topic, reason) VALUES (?, ?)",
            (topic, reason),
        )
        cid = cursor.lastrowid
    conn.close()
    return cid


def get_pending_curiosities(limit: int = 3) -> List[Dict]:
    """아직 해소되지 않은 궁금증 목록."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, topic, reason, created_at FROM curiosities WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def address_curiosity(curiosity_id: int) -> None:
    """궁금증을 해소됨으로 표시."""
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE curiosities SET status='addressed', addressed_at=datetime('now','localtime') WHERE id=?",
            (curiosity_id,),
        )
    conn.close()


def dismiss_curiosity(curiosity_id: int) -> None:
    """궁금증을 무시/폐기."""
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE curiosities SET status='dismissed', addressed_at=datetime('now','localtime') WHERE id=?",
            (curiosity_id,),
        )
    conn.close()


def render_curiosity_prompt(limit: int = 1) -> Optional[str]:
    """context 주입용 궁금증 한 줄 생성. 없으면 None."""
    items = get_pending_curiosities(limit)
    if not items:
        return None
    parts = []
    for item in items:
        line = f"#{item['id']} {item['topic']}"
        if item.get("reason"):
            line += f" ({item['reason']})"
        parts.append(line)
    return "[궁금증] " + " | ".join(parts)
