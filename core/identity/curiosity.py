"""
궁금증(curiosity) 큐 관리
반성 시 생성 → 다음 세션 시작 시 context에 주입 → 대화 후 addressed 처리
"""

from typing import Dict, List, Optional
from core.storage.db import get_connection
from core.common.sanitizer import sanitize


def add_curiosity(topic: str, reason: str = "", dedup: bool = True) -> int:
    """새 궁금증 추가. 생성된(또는 갱신된) id 반환.

    dedup=True면 같은 topic의 pending 궁금증이 이미 있을 때 새 행을 만들지 않고
    기존 행의 reason만 최신으로 갱신한다. 반성 이벤트처럼 topic이 고정된
    자동 생성 경로가 같은 내용을 수십 개씩 쌓는 걸 막는다.
    """
    topic = sanitize(topic, max_length=500)
    reason = sanitize(reason, max_length=500)
    conn = get_connection()
    try:
        with conn:
            if dedup:
                row = conn.execute(
                    "SELECT id FROM curiosities WHERE status='pending' AND topic=? ORDER BY id DESC LIMIT 1",
                    (topic,),
                ).fetchone()
                if row:
                    cid = row["id"]
                    if reason:
                        conn.execute("UPDATE curiosities SET reason=? WHERE id=?", (reason, cid))
                    return cid
            cursor = conn.execute(
                "INSERT INTO curiosities (topic, reason) VALUES (?, ?)",
                (topic, reason),
            )
            return cursor.lastrowid
    finally:
        conn.close()


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


DEFAULT_CURIOSITY_TTL_DAYS = 14
DEFAULT_PROCESSED_RETENTION_DAYS = 30


def purge_processed_curiosities(retention_days: int = DEFAULT_PROCESSED_RETENTION_DAYS) -> int:
    """해소/폐기된 궁금증 중 오래된 것을 삭제하고 건수를 반환한다.

    처리된 행을 읽는 곳은 대시보드 이력 뷰뿐이라 무한정 남길 이유가 없다.
    retention_days=0 이면 처리된 것을 전부 삭제한다.
    """
    conn = get_connection()
    try:
        with conn:
            if retention_days <= 0:
                cursor = conn.execute("DELETE FROM curiosities WHERE status IN ('addressed','dismissed')")
            else:
                cursor = conn.execute(
                    """DELETE FROM curiosities
                       WHERE status IN ('addressed','dismissed')
                         AND COALESCE(addressed_at, created_at) < datetime('now','localtime', ?)""",
                    (f"-{int(retention_days)} days",),
                )
            return cursor.rowcount or 0
    finally:
        conn.close()


def expire_stale_curiosities(ttl_days: int = DEFAULT_CURIOSITY_TTL_DAYS) -> int:
    """오래도록 다뤄지지 않은 pending 궁금증을 폐기하고 건수를 반환한다.

    해소는 대화에서 실제로 다뤄져야 일어나므로, 영영 안 다뤄지는 항목이 큐에
    남으면 context에는 늘 같은 것만 주입돼 오히려 새 궁금증을 가린다.
    """
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE curiosities
                   SET status='dismissed', addressed_at=datetime('now','localtime')
                   WHERE status='pending'
                     AND created_at < datetime('now','localtime', ?)""",
                (f"-{int(ttl_days)} days",),
            )
            return cursor.rowcount or 0
    finally:
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

