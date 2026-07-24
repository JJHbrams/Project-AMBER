"""STM → LTM 승격 파이프라인.

overlay 세션 종료 시 scope='overlay' 최근 대화를 Ollama로 요약 → memories 저장.

신호 3개 weighted sum (합계 1.0):
  novelty  (0.25): 기존 기억과 얼마나 다른가  — fuzzy triangular membership
  activity (0.30): 대화량 (user 턴 수 기반)
  recency  (0.45): 마지막 승격 이후 경과 시간

score >= 0.5 이면 Ollama(qwen2.5:1.5b) 요약 → save_memory()
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from core.storage.db import get_connection
from core.memory.store import save_memory, upsert_working_memory
from .semantic_graph import get_semantic_graph

logger = logging.getLogger(__name__)

_PROMOTE_TS_FILE = Path.home() / ".engram" / "_stm_promote_ts.json"
_OVERLAY_USER_CONFIG = Path.home() / ".engram" / "overlay.user.yaml"
_OLLAMA_MODEL = "qwen2.5:1.5b"
_OLLAMA_TIMEOUT = 30


def _ollama_base_url() -> str:
    """~/.engram/overlay.user.yaml의 cli.ollama_base_url을 우선 사용한다.

    overlay가 원격 Ollama(예: DGX)로 설정된 경우 localhost 하드코딩은
    항상 연결 실패로 이어지므로, 설정된 값을 그대로 따른다.
    """
    try:
        import yaml

        with open(_OVERLAY_USER_CONFIG, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        url = (data.get("cli") or {}).get("ollama_base_url")
        if url:
            return str(url).rstrip("/")
    except Exception:
        pass
    return "http://localhost:11434"


def _ollama_generate_url() -> str:
    return f"{_ollama_base_url()}/api/generate"


def _ollama_tags_url() -> str:
    return f"{_ollama_base_url()}/api/tags"


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
        data[scope_key] = datetime.now().isoformat()
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
    """대화 centroid와 기존 기억 유사도 기반 novelty (0=기존과 유사, 1=새로움)."""
    try:
        text = " ".join(m["content"][:150] for m in msgs[-8:])
        sg = get_semantic_graph()
        conv_vec = sg.compute_embedding(text)
        if sg.enabled:
            results = sg.episode_semantic_search(query_vec=conv_vec, top_k=3)
            if results:
                max_sim = max(r.get("score", 0.0) for r in results)
                return _novelty_membership(1.0 - max_sim)
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


# ── Ollama ───────────────────────────────────────────────────────────────────


def _ollama_available() -> bool:
    try:
        return requests.get(_ollama_tags_url(), timeout=3).status_code == 200
    except Exception:
        return False


def _summarize_with_ollama(msgs: list[dict]) -> Optional[str]:
    lines = []
    for m in msgs[-12:]:
        role = "사용자" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content'][:200]}")
    prompt = (
        "다음 대화에서 기억할 핵심 정보만 1~2문장으로 정리해줘. "
        "결정사항, 중요한 사실, 새로 알게 된 내용 위주로.\n\n" + "\n".join(lines) + "\n\n요약:"
    )
    try:
        resp = requests.post(
            _ollama_generate_url(),
            json={"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=_OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip() or None
    except Exception as e:
        logger.warning("Ollama 요약 실패 (%s): %s", _OLLAMA_MODEL, e)
        return None


# ── Working memory (세션 종료 시 항상 갱신 — novelty/score 게이팅 없음) ────


_WM_OLLAMA_TIMEOUT = 20
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


def _summarize_working_memory_with_ollama(msgs: list[dict]) -> Optional[dict]:
    """세션 요약 + 다음 작업(open_intents)을 로컬 Ollama로 생성한다.

    engram_close_session이 모델 호출에 의존해 불안정한 것과 달리, 이건
    watchdog이 세션 종료를 감지한 시점에 자동으로 실행된다 — Claude API
    토큰 소모 없이 로컬 모델(qwen2.5:1.5b)만 사용.
    """
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
    try:
        resp = requests.post(
            _ollama_generate_url(),
            json={"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=_WM_OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
    except Exception as e:
        logger.warning("working_memory Ollama 요약 실패: %s", e)
        return None
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
    """세션 종료 시 항상 실행 — 로컬 Ollama로 요약해 working_memory를 갱신한다."""
    msgs = _get_recent_messages_for_scope(scope_key)
    if not msgs:
        logger.debug("working_memory update skip: 메시지 없음 (scope=%s)", scope_key)
        return False
    if not _ollama_available():
        logger.warning("Ollama 응답 없음 — working_memory 갱신 스킵 (scope=%s)", scope_key)
        return False

    result = _summarize_working_memory_with_ollama(msgs)
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


def maybe_promote(scope_key: str = "overlay") -> bool:
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

    if not _ollama_available():
        logger.warning("Ollama 응답 없음 — STM 승격 스킵 (score=%.2f)", score)
        return False

    summary = _summarize_with_ollama(msgs)
    if not summary:
        return False

    save_memory(None, f"[overlay] {summary}")
    _set_last_promoted_ts(scope_key)
    logger.info("STM→LTM 승격 완료 (score=%.2f): %s", score, summary[:80])
    return True


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

_REFLECTION_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "detected": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["detected", "note"],
}

_REFLECTION_SESSION_FILE = Path.home() / ".engram" / "_reflection_session.json"


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


def _detect_reflection_event_with_claude(msgs: list[dict]) -> Optional[str]:
    """resume 가능한 Claude 세션으로 있음/없음 + 근거를 JSON 스키마로 받는다.

    이 세션은 매번 resume되므로 과거 회차의 판단 이력이 누적된다 — 페르소나
    발달 판단을 위한 자기만의 지속적 기록이 되는 셈(사용자의 원래 의도인
    "대화를 통한 자율적 성격 발달"에 더 가까움).
    """
    from core.identity.reflection_client import call_claude_resumable

    lines = []
    for m in msgs:
        role = "사용자" if m["role"] == "user" else "AI"
        lines.append(f"{role}: {m['content'][:300]}")
    prompt = (
        "다음은 방금 끝난 대화의 일부다. AI 자신의 성격·말투·행동 방식에 대해 "
        "사용자가 직접 의견을 냈거나(칭찬/지적 모두 포함), 대화에 특이하게 강한 "
        "감정적 텐션이 있었거나, AI가 자기 자신에 대해 몰랐던 걸 알게 된 순간이 "
        "있는지 판단해라. detected는 true/false, note는 감지됐을 때만 한 문장으로 "
        "무슨 일이 있었는지, 없으면 빈 문자열.\n\n" + "\n".join(lines)
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

    if not isinstance(parsed, dict) or not parsed.get("detected"):
        return None
    note = str(parsed.get("note", "")).strip()
    return note[:300] if note else None


def flag_reflection_event_from_recent_session(scope_key: str = "overlay") -> bool:
    """세션 종료 시 실행 — 반성할 만한 이벤트가 있으면 curiosity로 남겨 다음 세션에 넘긴다.

    narrative/persona는 여기서 절대 직접 수정하지 않는다 — 그건 실제 대화 중인
    모델의 자율 판단 몫이다. 이 함수는 오직 "이런 게 있었다"는 신호만 남긴다.
    """
    msgs = _get_recent_messages_for_scope(scope_key)
    if not msgs:
        return False

    note = _detect_reflection_event_with_claude(msgs)
    if not note:
        return False

    from core.identity.curiosity import add_curiosity

    add_curiosity(
        topic="지난 세션에서 반성할 만한 순간이 있었어",
        reason=note,
    )
    logger.info("reflection event 감지 → curiosity 등록 (scope=%s): %s", scope_key, note[:80])
    return True


