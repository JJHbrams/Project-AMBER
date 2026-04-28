"""
세션 종료 후 자율 반성 엔진.
- prepare: 대화 이력 + 정체성 + 테마를 수집하여 반성 컨텍스트 반환
- apply: 반성 결과(새 narrative, 요약)를 DB에 적용 + 테마 감쇠

MCP 서버를 통해 호출 도구가 직접 반성의 주체가 됨.
~~독립 REPL(engram.py)에서는 ask_llm()을 통해 반성을 수행.~~ (레거시, 미사용)
"""

from .db import get_connection
from .identity import get_identity, update_narrative, decay_themes, get_themes


def prepare_reflection_context(session_id: int) -> dict:
    """세션 대화 이력 + 현재 정체성 + 테마를 수집하여 반성 컨텍스트를 반환."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
        (session_id,),
    ).fetchall()
    conn.close()

    if not rows:
        return {"conversation": [], "identity": {}, "themes": [], "message_count": 0}

    conversation = [{"role": r["role"], "content": r["content"]} for r in rows]
    identity = get_identity()
    themes = get_themes(10)

    return {
        "current_identity": {
            "name": identity.get("name", "연속체"),
            "narrative": identity.get("narrative", ""),
        },
        "themes": [{"name": n, "weight": round(w, 2)} for n, w in themes],
        "conversation": conversation,
        "message_count": len(conversation),
    }


def apply_reflection(session_id: int, new_narrative: str, reflection_summary: str):
    """반성 결과를 적용: narrative 업데이트 + 세션 요약 저장 + 테마 감쇠."""
    update_narrative(new_narrative)
    _save_session_summary(session_id, reflection_summary)
    decay_themes()


def run_reflection(session_id: int) -> str:
    """[LEGACY] 독립 REPL(engram.py)용 반성 함수. 현재 미사용.
    MCP 경로(engram_prepare_reflection / engram_apply_reflection)를 사용할 것."""
    raise NotImplementedError(
        "run_reflection은 레거시 REPL 전용입니다. "
        "MCP 도구 engram_prepare_reflection / engram_apply_reflection을 사용하세요."
    )


def _save_session_summary(session_id: int, summary: str):
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE sessions SET summary=?, ended_at=datetime('now','localtime') WHERE id=?",
            (summary, session_id),
        )
    conn.close()
