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
from core.memory.store import save_memory
from .semantic_graph import get_semantic_graph

logger = logging.getLogger(__name__)

_PROMOTE_TS_FILE = Path.home() / ".engram" / "_stm_promote_ts.json"
_OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
_OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
_OLLAMA_MODEL = "qwen2.5:1.5b"
_OLLAMA_TIMEOUT = 30


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
        return requests.get(_OLLAMA_TAGS_URL, timeout=3).status_code == 200
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
            _OLLAMA_GENERATE_URL,
            json={"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=_OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip() or None
    except Exception as e:
        logger.warning("Ollama 요약 실패 (%s): %s", _OLLAMA_MODEL, e)
        return None


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


