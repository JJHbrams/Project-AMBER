"""STM → LTM 승격 파이프라인.

overlay 세션 종료 시 scope='overlay' 최근 대화를 Claude Code로 요약 → memories 저장.

신호 3개 weighted sum (합계 1.0):
  novelty  (0.25): 기존 기억과 얼마나 다른가  — fuzzy triangular membership
  activity (0.30): 대화량 (user 턴 수 기반)
  recency  (0.45): 마지막 승격 이후 경과 시간

score >= 0.5 이면 Claude Code 단발 요약 → save_memory()
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.storage.db import get_connection
from core.memory.store import save_memory, upsert_working_memory
from .semantic_graph import get_semantic_graph, run_sg_coro

logger = logging.getLogger(__name__)

_PROMOTE_TS_FILE = Path.home() / ".engram" / "_stm_promote_ts.json"
_AUTO_CHECKPOINT_STATE_FILE = Path.home() / ".engram" / "_auto_checkpoint_state.json"
_AUTO_CHECKPOINT_LOCK = threading.Lock()


# ── Triangular membership + COG defuzz ─────────────────────────────────────


def _trimf(x: float, a: float, b: float, c: float) -> float:
    if x <= a or x >= c:
        return 0.0
    if x <= b:
        return (x - a) / (b - a)
    return (c - x) / (c - b)


def _novelty_membership(cosine_dist: float) -> float:
    """코사인 거리(0~1) → fuzzy novelty score(0~1), COG defuzz."""
    low = max(0.0, 1.0 - cosine_dist / 0.35)
    medium = _trimf(cosine_dist, 0.2, 0.5, 0.8)
    high = max(0.0, (cosine_dist - 0.5) / 0.5)
    total = low + medium + high
    if total < 1e-9:
        return 0.5
    return (0.1 * low + 0.5 * medium + 0.9 * high) / total


# ── Promotion state ─────────────────────────────────────────────────────────


def _get_last_promoted_ts(scope_key: str) -> Optional[str]:
    try:
        data = json.loads(_PROMOTE_TS_FILE.read_text())
        return data.get(scope_key)
    except Exception:
        return None


def _set_last_promoted_ts(scope_key: str) -> None:
    try:
        data: dict = {}
        try:
            data = json.loads(_PROMOTE_TS_FILE.read_text())
        except Exception:
            pass
        # sqlite의 datetime('now','localtime') 포맷과 맞춘다 — ISO 'T' 구분자로
        # 저장하면 m.timestamp > last_ts 문자열 비교가 같은 날 메시지를 전부
        # 걸러버린다(' ' < 'T'). fromisoformat은 공백 구분자도 파싱한다.
        data[scope_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = _PROMOTE_TS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _PROMOTE_TS_FILE)
    except Exception as e:
        logger.warning("promote ts 저장 실패: %s", e)


# ── Message retrieval ────────────────────────────────────────────────────────


def _get_promotable_messages(scope_key: str, max_minutes: int = 240) -> list[dict]:
    """마지막 승격 이후 + max_minutes 범위 내 메시지만 반환 (중복 방지)."""
    last_ts = _get_last_promoted_ts(scope_key)
    conn = get_connection()
    try:
        if last_ts:
            rows = conn.execute(
                """SELECT m.role, m.content
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE s.scope_key = ?
                     AND m.timestamp > ?
                     AND m.timestamp >= datetime('now','localtime', ?)
                   ORDER BY m.timestamp ASC LIMIT 100""",
                (scope_key, last_ts, f"-{max_minutes} minutes"),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.role, m.content
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE s.scope_key = ?
                     AND m.timestamp >= datetime('now','localtime', ?)
                   ORDER BY m.timestamp ASC LIMIT 100""",
                (scope_key, f"-{max_minutes} minutes"),
            ).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# ── Signal computation ───────────────────────────────────────────────────────


def _compute_novelty(msgs: list[dict]) -> float:
    """대화 centroid와 기존 기억 유사도 기반 novelty (0=기존과 유사, 1=새로움).

    호출부(maybe_promote)가 이벤트 루프 없는 스레드(overlay 메인 tkinter 스레드 —
    bubble 세션 자체 루프는 이 시점엔 이미 join되어 종료된 상태)에서 실행되므로
    run_sg_coro로 감싼다."""
    try:
        text = " ".join(m["content"][:150] for m in msgs[-8:])
        sg = get_semantic_graph()
        conv_vec = run_sg_coro(sg.compute_query_embedding(text))
        if sg.enabled:
            results = run_sg_coro(sg.episode_semantic_search(query_vec=conv_vec, top_k=3))
            if results:
                max_sim = max(r.get("score", 0.0) for r in results)
                return _novelty_membership(sg.novelty_distance(max_sim))
    except Exception as e:
        logger.debug("novelty 계산 실패 (중립 0.5 사용): %s", e)
    return 0.5


def _compute_activity(msgs: list[dict]) -> float:
    """user 턴 수 기반 활동량 (8턴이면 1.0)."""
    user_turns = sum(1 for m in msgs if m["role"] == "user")
    return min(user_turns / 8.0, 1.0)


def _compute_recency(scope_key: str) -> float:
    """마지막 승격 이후 경과 시간 (6시간이면 1.0, 처음이면 1.0)."""
    last_ts = _get_last_promoted_ts(scope_key)
    if not last_ts:
        return 1.0
    try:
        hours = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() / 3600
        return min(hours / 6.0, 1.0)
    except Exception:
        return 1.0


def _compute_score(novelty: float, activity: float, recency: float) -> float:
    return novelty * 0.25 + activity * 0.30 + recency * 0.45


# ── Claude Code summary ──────────────────────────────────────────────────────


def _call_claude_once(prompt: str, timeout: float) -> Optional[str]:
    """기존 Claude Code OAuth 인증을 사용하는 안전 모드 단발 호출."""
    from core.identity.reflection_client import call_claude_resumable

    text, _session_id = call_claude_resumable(prompt, timeout=timeout)
    return text.strip() if text and text.strip() else None


def _summarize_with_claude(msgs: list[dict]) -> Optional[str]:
    lines = []
    for m in msgs[-12:]:
        role = "사용자" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content'][:200]}")
    prompt = (
        "다음 대화에서 기억할 핵심 정보만 1~2문장으로 정리해줘. "
        "결정사항, 중요한 사실, 새로 알게 된 내용 위주로.\n\n" + "\n".join(lines) + "\n\n요약:"
    )
    return _call_claude_once(prompt, timeout=60.0)


# ── Working memory (세션 종료 시 항상 갱신 — novelty/score 게이팅 없음) ────


_WM_MAX_MESSAGES = 40


def _get_recent_messages_for_scope(scope_key: str, limit: int = _WM_MAX_MESSAGES) -> list[dict]:
    """스코프의 가장 최근 세션(방금 끝난 세션)에서 최근 메시지를 가져온다."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT m.role, m.content
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
               WHERE s.scope_key = ?
               ORDER BY m.timestamp DESC LIMIT ?""",
            (scope_key, limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _summarize_working_memory_with_claude(msgs: list[dict]) -> Optional[dict]:
    """세션 요약 + 다음 작업(open_intents)을 Claude Code 단발 호출로 생성한다."""
    lines = []
    for m in msgs:
        role = "사용자" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content'][:300]}")
    prompt = (
        "다음은 방금 끝난 대화 세션이다. 정확히 두 줄로만 답해라.\n"
        "1번째 줄: '요약: ' 뒤에 이번 세션에서 한 일을 1~2문장으로.\n"
        "2번째 줄: '다음 작업: ' 뒤에 아직 안 끝난 일이 있으면 적고, 없으면 '없음'.\n\n"
        + "\n".join(lines)
        + "\n\n답변:"
    )
    text = _call_claude_once(prompt, timeout=60.0)
    if not text:
        return None

    summary = ""
    open_intents = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("요약:"):
            summary = line[len("요약:"):].strip()
        elif line.startswith("다음 작업:"):
            val = line[len("다음 작업:"):].strip()
            open_intents = "" if val in ("없음", "-", "") else val
    if not summary:
        # 파싱 실패 시 원문 전체를 summary로 (완전히 버리는 것보단 나음)
        summary = text[:500]
    return {"summary": summary, "open_intents": open_intents}


def update_working_memory_from_recent_session(scope_key: str = "overlay") -> bool:
    """세션 종료 시 항상 실행 — Claude Code로 요약해 working_memory를 갱신한다."""
    msgs = _get_recent_messages_for_scope(scope_key)
    if not msgs:
        logger.debug("working_memory update skip: 메시지 없음 (scope=%s)", scope_key)
        return False

    result = _summarize_working_memory_with_claude(msgs)
    if not result:
        return False

    upsert_working_memory(scope_key, result["summary"], open_intents=result["open_intents"])
    logger.info("working_memory 갱신 완료 (scope=%s): %s", scope_key, result["summary"][:80])
    return True


def update_working_memory_from_recent_session_async(scope_key: str = "overlay") -> threading.Thread:
    t = threading.Thread(target=update_working_memory_from_recent_session, args=(scope_key,), daemon=True)
    t.start()
    return t


# ── Main entry point ─────────────────────────────────────────────────────────


def maybe_promote(
    scope_key: str = "overlay",
    *,
    summary_override: str = "",
    project: str = "",
    session_id: int | None = None,
    source: str = "save",
) -> bool:
    """STM → LTM 승격 시도. 승격 발생 시 True 반환."""
    msgs = _get_promotable_messages(scope_key)
    if not msgs:
        logger.debug("promote skip: 새 메시지 없음 (scope=%s)", scope_key)
        return False

    novelty = _compute_novelty(msgs)
    activity = _compute_activity(msgs)
    recency = _compute_recency(scope_key)
    score = _compute_score(novelty, activity, recency)

    logger.info(
        "promote score=%.2f (novelty=%.2f act=%.2f rec=%.2f) msgs=%d scope=%s",
        score,
        novelty,
        activity,
        recency,
        len(msgs),
        scope_key,
    )

    if score < 0.5:
        logger.debug("promote skip: score=%.2f < 0.5", score)
        return False

    summary = summary_override.strip() or _summarize_with_claude(msgs)
    if not summary:
        return False

    save_memory(
        session_id,
        f"[overlay] {summary}",
        source=source,
        project=project,
    )
    _set_last_promoted_ts(scope_key)
    logger.info("STM→LTM 승격 완료 (score=%.2f): %s", score, summary[:80])
    return True


def _load_auto_checkpoint_state() -> dict:
    try:
        data = json.loads(_AUTO_CHECKPOINT_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_auto_checkpoint_state(data: dict) -> None:
    _AUTO_CHECKPOINT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _AUTO_CHECKPOINT_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _AUTO_CHECKPOINT_STATE_FILE)


def _get_auto_checkpoint_candidate(
    scope_key: str,
    *,
    idle_seconds: int,
    min_user_turns: int,
) -> dict | None:
    state = _load_auto_checkpoint_state().get(scope_key, {})
    last_message_id = int(state.get("last_message_id", 0) or 0)
    conn = get_connection()
    try:
        session = conn.execute(
            """SELECT id FROM sessions
               WHERE scope_key = ? AND ended_at IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (scope_key,),
        ).fetchone()
        if not session:
            return None
        memory_watermark = conn.execute(
            """SELECT MAX(ts) AS ts FROM (
                   SELECT MAX(created_at) AS ts FROM memories WHERE session_id = ?
                   UNION ALL
                   SELECT MAX(updated_at) AS ts FROM working_memory WHERE scope_key = ?
               )""",
            (session["id"], scope_key),
        ).fetchone()
        cutoff_ts = str(memory_watermark["ts"] or "") if memory_watermark else ""
        rows = conn.execute(
            """SELECT id, role, content, timestamp
               FROM messages
               WHERE session_id = ? AND id > ?
                 AND (? = '' OR timestamp > ?)
               ORDER BY id DESC LIMIT 200""",
            (session["id"], last_message_id, cutoff_ts, cutoff_ts),
        ).fetchall()
    finally:
        conn.close()
    rows = list(reversed(rows))
    if not rows or rows[-1]["role"] != "assistant":
        return None
    user_turns = sum(1 for row in rows if row["role"] == "user")
    if user_turns < min_user_turns:
        return None
    try:
        idle_for = (datetime.now() - datetime.fromisoformat(rows[-1]["timestamp"])).total_seconds()
    except (TypeError, ValueError):
        return None
    if idle_for < idle_seconds:
        return None
    return {
        "session_id": int(session["id"]),
        "last_message_id": int(rows[-1]["id"]),
        "messages": [{"role": row["role"], "content": row["content"]} for row in rows],
        "user_turns": user_turns,
    }


def maybe_auto_checkpoint(
    scope_key: str = "overlay",
    *,
    cwd: str = "",
    idle_seconds: int = 1800,
    min_user_turns: int = 5,
    external_daily_dir: str = "",
) -> dict:
    """유휴 중인 열린 세션을 닫지 않고 기억·일지를 체크포인트한다."""
    if not _AUTO_CHECKPOINT_LOCK.acquire(blocking=False):
        return {"status": "busy"}
    try:
        candidate = _get_auto_checkpoint_candidate(
            scope_key,
            idle_seconds=max(60, int(idle_seconds)),
            min_user_turns=max(1, int(min_user_turns)),
        )
        if candidate is None:
            return {"status": "skipped"}

        result = _summarize_working_memory_with_claude(candidate["messages"])
        if not result:
            return {"status": "summary_failed"}

        from core.context.project_scope import resolve_kg_node_id, resolve_project_key
        from core.memory.daily_checkpoint import append_daily_checkpoint
        from core.observability.activity import log_activity

        project_key = resolve_project_key(cwd=cwd)
        project_node_id = resolve_kg_node_id(project_key) if project_key else None
        upsert_working_memory(
            scope_key,
            result["summary"],
            open_intents=result["open_intents"],
        )
        promoted = maybe_promote(
            scope_key,
            summary_override=result["summary"],
            project=project_key,
            session_id=candidate["session_id"],
            source="auto-checkpoint",
        )
        checkpoint_id = (
            f"{scope_key}-{candidate['session_id']}-{candidate['last_message_id']}"
        )
        note_result = append_daily_checkpoint(
            checkpoint_id=checkpoint_id,
            now=datetime.now().astimezone(),
            summary=result["summary"],
            open_intents=result["open_intents"],
            project_key=project_key,
            project_node_id=project_node_id,
            external_daily_dir=external_daily_dir,
        )
        log_activity(
            actor="auto-checkpoint",
            project=project_key,
            action=result["summary"],
            detail=(
                f"checkpoint={checkpoint_id}; user_turns={candidate['user_turns']}; "
                f"ltm_promoted={promoted}"
            ),
        )
        state = _load_auto_checkpoint_state()
        state[scope_key] = {
            "last_message_id": candidate["last_message_id"],
            "checkpoint_id": checkpoint_id,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        _save_auto_checkpoint_state(state)
        logger.info(
            "auto checkpoint 완료 scope=%s turns=%d promoted=%s checkpoint=%s",
            scope_key,
            candidate["user_turns"],
            promoted,
            checkpoint_id,
        )
        return {
            "status": "checkpointed",
            "checkpoint_id": checkpoint_id,
            "ltm_promoted": promoted,
            **note_result,
        }
    except Exception:
        logger.exception("auto checkpoint 실패 (scope=%s)", scope_key)
        return {"status": "failed"}
    finally:
        _AUTO_CHECKPOINT_LOCK.release()


def maybe_promote_async(scope_key: str = "overlay") -> threading.Thread:
    """maybe_promote()를 백그라운드 스레드에서 실행하고 Thread 객체를 반환한다."""
    t = threading.Thread(target=maybe_promote, args=(scope_key,), daemon=True)
    t.start()
    return t


# ── 반성 이벤트 감지 (페르소나 자율 발달용) ─────────────────────────────────
#
# 목표는 "매 세션 강제 반성"이 아니라 "반성할 만한 이벤트를 놓치지 않는 것".
# 판단(있음/없음 + 근거)은 curiosity로 다음 세션에 넘기고, 실제 narrative/persona
# 반영은 대화 중인 진짜 Claude의 완전한 자율 판단에 맡긴다 — 여기선 절대 직접
# update_narrative/update_persona를 호출하지 않는다.
#
# 로컬 Ollama(qwen2.5:1.5b)로는 이 판단(단순 요약보다 미묘한 분류) 신뢰도가
# 낮았음(포맷 무시, 장황한 메타분석). 이 판단은 세션 종료마다 1회뿐이라 빈도가
# 낮으므로, 새 API 키 없이 이미 인증된 claude CLI 세션(OAuth/구독)을 resume해서
# 재사용 — Claude 품질 판단을 얻으면서 새 키 관리 표면은 늘리지 않는다.

# 같은 호출에서 테마(관심사 라벨)도 함께 받는다 — 모델은 이미 대화 전문을 보고
# 있으므로 추가 호출 없이 의미 단위 라벨을 얻을 수 있다. 예전엔 정규식으로 한글
# 명사를 주워 담아 "바탕으로", "완료" 같은 게 관심사 자리에 올라왔다.
_REFLECTION_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "detected": {"type": "boolean"},
        "note": {"type": "string"},
        "themes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
        },
        # 해소된 궁금증 id — 생성만 자동이고 해소는 모델의 자발적 tool 호출에만
        # 의존하던 비대칭 때문에, 전체 이력에서 addressed가 1건뿐이었다.
        "addressed_curiosity_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
    },
    "required": ["detected", "note", "themes", "addressed_curiosity_ids"],
}

_REFLECTION_SESSION_FILE = Path.home() / ".engram" / "_reflection_session.json"
_REFLECT_TS_FILE = Path.home() / ".engram" / "_reflection_ts.json"

# watchdog(죽은 PID마다 1회)과 stm_bridge.close()가 같은 종료에 대해 동시에
# 들어오는 경우가 있어 프로세스 내 직렬화 — 워터마크 read-modify-write 보호.
_REFLECT_LOCK = threading.Lock()


def _get_last_reflect_ts(scope_key: str) -> Optional[str]:
    try:
        data = json.loads(_REFLECT_TS_FILE.read_text())
        return data.get(scope_key)
    except Exception:
        return None


def _set_last_reflect_ts(scope_key: str, ts: str) -> None:
    """마지막으로 판정한 메시지 시각을 기록 (sqlite datetime 포맷 그대로)."""
    try:
        data: dict = {}
        try:
            data = json.loads(_REFLECT_TS_FILE.read_text())
        except Exception:
            pass
        data[scope_key] = ts
        _REFLECT_TS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REFLECT_TS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _REFLECT_TS_FILE)
    except Exception as e:
        logger.warning("reflection ts 저장 실패: %s", e)


def _get_reflectable_messages(scope_key: str, limit: int = _WM_MAX_MESSAGES) -> list[dict]:
    """마지막 판정 이후에 새로 쌓인 메시지만 반환.

    워터마크가 없으면 같은 대화를 종료 때마다 다시 판정해 동일한 curiosity가
    반복 생성된다 — 실제로 그 버그로 같은 항목이 17개까지 쌓였다.
    """
    last_ts = _get_last_reflect_ts(scope_key)
    conn = get_connection()
    try:
        if last_ts:
            rows = conn.execute(
                """SELECT m.role, m.content, m.timestamp
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE s.scope_key = ? AND m.timestamp > ?
                   ORDER BY m.timestamp DESC LIMIT ?""",
                (scope_key, last_ts, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.role, m.content, m.timestamp
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE s.scope_key = ?
                   ORDER BY m.timestamp DESC LIMIT ?""",
                (scope_key, limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
        for r in reversed(rows)
    ]


def _get_reflection_session_id() -> Optional[str]:
    try:
        data = json.loads(_REFLECTION_SESSION_FILE.read_text())
        return data.get("persona_session_id")
    except Exception:
        return None


def _set_reflection_session_id(session_id: str) -> None:
    try:
        _REFLECTION_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REFLECTION_SESSION_FILE.write_text(json.dumps({"persona_session_id": session_id}))
    except Exception as e:
        logger.warning("reflection session_id 저장 실패: %s", e)


def _read_session_insight_with_claude(msgs: list[dict], pending: list[dict]) -> Optional[dict]:
    """resume 가능한 Claude 세션으로 반성 이벤트·관심사·해소된 궁금증을 한 번에 받는다.

    반환: {"note": str, "themes": list[str], "addressed": list[int]}
    note는 반성 이벤트가 없으면 "". 호출 실패/파싱 실패 시 None.

    이 세션은 매번 resume되므로 과거 회차의 판단 이력이 누적된다 — 페르소나
    발달 판단을 위한 자기만의 지속적 기록이 되는 셈(사용자의 원래 의도인
    "대화를 통한 자율적 성격 발달"에 더 가까움).
    """
    from core.identity.reflection_client import call_claude_resumable

    lines = []
    for m in msgs:
        role = "사용자" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content'][:300]}")

    pending_section = ""
    if pending:
        pending_lines = "\n".join(f"  #{c['id']} {c['topic']} ({c.get('reason', '')[:120]})" for c in pending)
        pending_section = (
            "3) 해소된 궁금증(addressed_curiosity_ids): 아래는 이전 세션에서 남긴 "
            "미해결 궁금증이다. 이번 대화에서 **실제로 다뤄져 해소된** 것의 id만 "
            "배열로 넣어라. 대화에 등장하지 않았거나 스치듯 언급만 됐으면 넣지 마라 "
            "— 확신이 없으면 넣지 않는 쪽이 맞다. 해소된 게 없으면 빈 배열.\n"
            f"{pending_lines}\n\n"
        )
    else:
        pending_section = (
            "3) addressed_curiosity_ids: 미해결 궁금증이 없으므로 빈 배열로 둬라.\n\n"
        )

    prompt = (
        "다음은 방금 끝난 대화의 일부다. 아래 세 가지를 판단해라.\n\n"
        "1) 반성 이벤트: AI 자신의 성격·말투·행동 방식에 대해 사용자가 직접 "
        "의견을 냈거나(칭찬/지적 모두 포함), 대화에 특이하게 강한 감정적 텐션이 "
        "있었거나, AI가 자기 자신에 대해 몰랐던 걸 알게 된 순간이 있는지. "
        "detected는 true/false, note는 감지됐을 때만 한 문장으로 무슨 일이 "
        "있었는지, 없으면 빈 문자열.\n\n"
        "2) 관심사(themes): 이 대화에서 드러난 '지속적 관심사'를 0~4개. "
        "명사 나열이 아니라 의미 단위 라벨이어야 한다 — 예: '기억 연속성', "
        "'말풍선 능동성', '설치 자립성'. 다음은 테마가 아니다: 작업 상태어"
        "('완료', '추가', '수정'), 조사가 붙은 어절, 그냥 언급된 파일·도구 이름. "
        "이번 대화가 잡담이거나 관심사라 할 게 없으면 빈 배열로 둬라. "
        "각 라벨은 20자 이내.\n\n"
        + pending_section
        + "--- 대화 ---\n"
        + "\n".join(lines)
    )
    session_id = _get_reflection_session_id()
    text, new_session_id = call_claude_resumable(prompt, session_id=session_id, json_schema=_REFLECTION_EVENT_SCHEMA)
    if new_session_id and new_session_id != session_id:
        _set_reflection_session_id(new_session_id)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("reflection event 응답 JSON 파싱 실패: %s", text[:200])
        return None
    if not isinstance(parsed, dict):
        return None

    raw_themes = parsed.get("themes")
    themes = [t for t in raw_themes if isinstance(t, str)] if isinstance(raw_themes, list) else []

    # 모델이 없는 id를 지어내도 실제 pending에 있는 것만 반영한다.
    pending_ids = {c["id"] for c in pending}
    raw_ids = parsed.get("addressed_curiosity_ids")
    addressed = (
        [i for i in raw_ids if isinstance(i, int) and i in pending_ids]
        if isinstance(raw_ids, list)
        else []
    )

    note = ""
    if parsed.get("detected"):
        note = str(parsed.get("note", "")).strip()[:300]
    return {"note": note, "themes": themes, "addressed": addressed}


def flag_reflection_event_from_recent_session(scope_key: str = "overlay") -> bool:
    """세션 종료 시 실행 — 반성 이벤트는 curiosity로, 관심사는 테마로 남긴다.

    narrative/persona는 여기서 절대 직접 수정하지 않는다 — 그건 실제 대화 중인
    모델의 자율 판단 몫이다. 이 함수는 오직 "이런 게 있었다"는 신호만 남긴다.
    반환값은 반성 이벤트 감지 여부(테마만 갱신된 경우는 False).
    """
    with _REFLECT_LOCK:
        msgs = _get_reflectable_messages(scope_key)
        if not msgs:
            logger.debug("reflection skip: 새 메시지 없음 (scope=%s)", scope_key)
            return False

        # 판정 성공/실패와 무관하게 워터마크를 먼저 전진시킨다 — 이벤트가 없던
        # 대화를 다음 종료 때 다시 훑지 않도록.
        _set_last_reflect_ts(scope_key, msgs[-1]["timestamp"])

        from core.identity.curiosity import (
            add_curiosity,
            address_curiosity,
            expire_stale_curiosities,
            get_pending_curiosities,
            purge_processed_curiosities,
        )

        pending = get_pending_curiosities(limit=5)
        insight = _read_session_insight_with_claude(msgs, pending)
        if not insight:
            return False

        if insight["themes"]:
            try:
                from core.identity import update_themes

                applied = update_themes(insight["themes"])
                if applied:
                    logger.info("테마 갱신 (scope=%s): %s", scope_key, ", ".join(applied))
            except Exception:
                logger.exception("테마 갱신 실패 (scope=%s)", scope_key)

        for cid in insight["addressed"]:
            try:
                address_curiosity(cid)
                logger.info("궁금증 #%d 해소 처리 (scope=%s)", cid, scope_key)
            except Exception:
                logger.exception("궁금증 #%d 해소 처리 실패", cid)

        # 아무도 다뤄주지 않은 채 오래 남은 건 자동 폐기 — 큐가 무한정 쌓이면
        # context에 늘 같은 항목만 주입돼 오히려 해소를 방해한다.
        try:
            expired = expire_stale_curiosities()
            if expired:
                logger.info("오래된 궁금증 %d건 자동 폐기", expired)
            purged = purge_processed_curiosities()
            if purged:
                logger.info("처리된 궁금증 %d건 삭제(보존기간 경과)", purged)
        except Exception:
            logger.exception("궁금증 정리 실패")

        note = insight["note"]
        if not note:
            return False

        add_curiosity(
            topic="지난 세션에서 반성할 만한 순간이 있었어",
            reason=note,
        )
        logger.info("reflection event 감지 → curiosity 등록 (scope=%s): %s", scope_key, note[:80])
        return True
