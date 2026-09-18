"""페르소나 예시(few-shot) 저장소.

형용사가 아니라 **실제로 오간 말**을 들고 있는 곳이다. `voice: 시니컬한 말투`는
어느 모델에게 줘도 같은 결과를 내지만, 실제로 했던 말 여덟 줄은 그렇지 않다.

스칼라(warmth·humor…)와 달리 여기 있는 것은 EMA·반성으로 **자동 진화하지 않는다**.
명시적 추가와 삭제만 있다. 개성은 평균이 아니라서 블렌딩하면 사라진다.

## 자극이 항상 사용자 입력인 것은 아니다

개성이 드러나는 발화의 상당수는 앞에 사용자 턴이 없다 — 코드를 읽고 나온 발견,
스모크 결과를 보고 하는 자기 정정, 수치를 재고 판단을 뒤집는 순간. 이것들이
하필 형용사로 제일 안 잡히는 것들이다.

그 자리를 억지로 user 턴으로 채우면 "사용자는 `(스모크 테스트 직후)` 같은 말을
한다"를 가르치게 된다. 그래서 자극에 종류를 붙이고 라벨을 드러내 렌더한다.
"""

import logging
from typing import Any, Dict, List, Optional

from core.common.sanitizer import sanitize
from core.storage.db import get_connection

_LOG = logging.getLogger(__name__)

# 렌더 예산 — 개수와 문자수를 동시에 건다. 어느 한쪽만 걸면 긴 예시 두 개가
# 컨텍스트를 다 먹거나, 짧은 예시 여덟 개가 아무것도 안 보여준다.
#
# 처음엔 8건/1,600자였는데 실측 평균이 196자/쌍이라 두 상한이 같은 지점에서
# 만났다 — 예산 상한이 아무 일도 하지 않았다는 뜻이다. 짧고 많은 쪽이 낫다:
# few-shot 은 다양성으로 일하고, 긴 예시는 길어지는 이유가 대개 고유명사와
# 구체적 사실이라 말투가 아니라 도메인을 옮긴다.
RENDER_LIMIT = 12
RENDER_CHAR_BUDGET = 1400

# 권장 길이를 넘는 쌍은 거부하지 않고 알려만 준다. 구조를 보여줘야 하는 예시는
# 길 수밖에 없고, 그 판단은 넣는 사람이 한다.
PAIR_CHARS_ADVISED = 300

# 자동 적재(반성)에만 거는 상한. 사람이 넣는 건 판단이 붙지만 자동은 안 붙는다.
AUTO_MAX_PAIR_CHARS = 150

ORIGIN_MANUAL = "manual"
ORIGIN_AUTO = "auto"

# 사람이 넣은 것이 자동으로 쌓인 것보다 먼저 뽑힌다. 교정이 한 번의 호출로
# 끝나야 하기 때문이다 — 마음에 안 들면 하나 넣으면 그게 자리를 가져간다.
_ORIGIN_WEIGHT = {ORIGIN_MANUAL: 1, ORIGIN_AUTO: 0}

# 한 태그가 렌더를 독식하지 못하게 하는 상한.
#
# 개성은 한 상황에서 나오지 않는다 — 상황이 바뀌어도 같은 사람인 것에서 나온다.
# 그래서 "진단" 예시만 여덟 개 있는 것보다 진단·반박·농담·거절이 섞인 여섯 개가 낫다.
# 태그가 하나뿐이면 이 상한 때문에 3개만 나간다. 의도된 동작이다 — 한 태그밖에
# 없다는 건 페르소나 표본이 아니라 한 가지 모드의 표본이라는 뜻이니까.
TAG_CAP = 3

# 자극의 종류와 렌더 라벨. 라벨을 드러내는 것이 핵심이다 — 감추면 모델이
# 상황 서술을 사용자 발화로 오해한다.
PROMPT_KINDS = {
    "user": "U",
    "situation": "상황",
    "source": "자료",
}
DEFAULT_PROMPT_KIND = "user"
RESPONSE_LABEL = "A"

# 태그가 없는 예시가 모이는 버킷. 다른 태그와 같은 상한을 받는다.
_UNTAGGED = "기타"

_MAX_SIDE = 1200  # 한쪽 발화의 저장 상한(문자)


def _row_to_example(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "prompt_kind": row["prompt_kind"],
        "prompt_text": row["prompt_text"],
        "response_text": row["response_text"],
        "tag": row["tag"],
        "source_turn_id": row["source_turn_id"],
        "weight": row["weight"],
        "active": bool(row["active"]),
        "origin": row["origin"],
        "created_at": row["created_at"],
    }


def add_example(
    prompt_text: str,
    response_text: str,
    prompt_kind: str = DEFAULT_PROMPT_KIND,
    tag: str = "",
    source_turn_id: Optional[int] = None,
    weight: Optional[int] = None,
    origin: str = ORIGIN_MANUAL,
) -> Dict[str, Any]:
    """예시 한 쌍을 저장한다. 호출 1회 = 사람이 승인한 pair 1건.

    sanitize 를 태우되 **변환 결과를 그대로 반환**한다 — 인젝션 패턴 치환이
    예시를 훼손할 수 있으므로, 무엇이 저장됐는지 사람이 보고 판단해야 한다.
    """
    if prompt_kind not in PROMPT_KINDS:
        raise ValueError(f"prompt_kind 는 {sorted(PROMPT_KINDS)} 중 하나여야 한다")
    if origin not in _ORIGIN_WEIGHT:
        raise ValueError(f"origin 은 {sorted(_ORIGIN_WEIGHT)} 중 하나여야 한다")
    if weight is None:
        weight = _ORIGIN_WEIGHT[origin]
    prompt_text = sanitize((prompt_text or "").strip(), max_length=_MAX_SIDE)
    response_text = sanitize((response_text or "").strip(), max_length=_MAX_SIDE)
    if not prompt_text or not response_text:
        raise ValueError("prompt_text 와 response_text 는 둘 다 필요하다")
    tag = sanitize((tag or "").strip(), max_length=40)

    advice = None
    pair_chars = len(prompt_text) + len(response_text)
    if pair_chars > PAIR_CHARS_ADVISED:
        advice = (
            f"쌍이 {pair_chars}자다(권장 {PAIR_CHARS_ADVISED}자 이하). "
            "길어지는 이유가 고유명사·날짜·변수명이라면 말투가 아니라 도메인을 가르치게 된다. "
            "동작은 남기고 사실은 덜어낼 것."
        )

    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO persona_examples
                   (prompt_kind, prompt_text, response_text, tag,
                    source_turn_id, weight, origin)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (prompt_kind, prompt_text, response_text, tag,
                 source_turn_id, int(weight), origin),
            )
            row = conn.execute(
                "SELECT * FROM persona_examples WHERE id=?", (cur.lastrowid,)
            ).fetchone()
    finally:
        conn.close()
    saved = _row_to_example(row)
    if advice:
        saved["advice"] = advice
    return saved


def list_examples(
    include_retired: bool = False, tag: str = "", origin: str = "", limit: int = 200
) -> List[Dict[str, Any]]:
    """저장된 예시를 가중치·최근 순으로 반환한다."""
    where, params = [], []
    if not include_retired:
        where.append("active = 1")
    if tag:
        where.append("tag = ?")
        params.append(tag)
    if origin:
        where.append("origin = ?")
        params.append(origin)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT * FROM persona_examples {clause}
                ORDER BY weight DESC, id DESC LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_example(r) for r in rows]


def _set_active(example_id: int, active: int) -> bool:
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE persona_examples SET active=? WHERE id=?", (active, int(example_id))
            )
    finally:
        conn.close()
    return cur.rowcount > 0


def retire_example(example_id: int) -> bool:
    """렌더에서 뺀다. 행은 남긴다 — 왜 뺐는지 되짚을 수 있어야 한다."""
    return _set_active(example_id, 0)


def restore_example(example_id: int) -> bool:
    return _set_active(example_id, 1)


def delete_example(example_id: int) -> bool:
    """영구 삭제. 되돌릴 수 없다."""
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM persona_examples WHERE id=?", (int(example_id),)
            )
    finally:
        conn.close()
    return cur.rowcount > 0


def select_balanced(
    rows: List[Dict[str, Any]], limit: int = RENDER_LIMIT, tag_cap: int = TAG_CAP
) -> List[Dict[str, Any]]:
    """태그별 라운드로빈으로 고른다.

    같은 태그를 연달아 담지 않으므로, 예시가 한쪽에 몰려 있어도 렌더는 고르게 나간다.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row["tag"] or _UNTAGGED, []).append(row)

    order = sorted(buckets)
    picked: List[Dict[str, Any]] = []
    for depth in range(tag_cap):
        for tag in order:
            if len(picked) >= limit:
                return picked
            bucket = buckets[tag]
            if depth < len(bucket):
                picked.append(bucket[depth])
    return picked


def tag_capacity(tag_cap: int = TAG_CAP) -> Dict[str, int]:
    """태그별 남은 자리. 자동 적재가 "빈 칸만 채운다"를 판단하는 근거다.

    어느 태그가 채워지는지는 규칙으로 정하지 않는다 — 무엇을 기억할 만하다고
    여겼는지가 성격이므로, 내용을 지정하면 기록이 아니라 지시가 된다.
    """
    used: Dict[str, int] = {}
    for row in list_examples():
        used[row["tag"] or _UNTAGGED] = used.get(row["tag"] or _UNTAGGED, 0) + 1
    return {tag: max(0, tag_cap - count) for tag, count in used.items()}


def has_room_for_tag(tag: str, tag_cap: int = TAG_CAP) -> bool:
    return tag_capacity(tag_cap).get(tag or _UNTAGGED, tag_cap) > 0


def format_pair(example: Dict[str, Any]) -> str:
    label = PROMPT_KINDS.get(example.get("prompt_kind"), PROMPT_KINDS[DEFAULT_PROMPT_KIND])
    return (
        label + ": " + example["prompt_text"] + "\n"
        + RESPONSE_LABEL + ": " + example["response_text"]
    )


def render_examples(
    limit: int = RENDER_LIMIT,
    char_budget: int = RENDER_CHAR_BUDGET,
    tag_cap: int = TAG_CAP,
) -> str:
    """컨텍스트 주입용 예시 블록 본문. 없으면 빈 문자열 — 빈 블록은 주입하지 않는다.

    캐시하지 않는다. retire·delete 가 다음 렌더에 바로 반영돼야 한다.
    """
    try:
        rows = list_examples(limit=max(limit * tag_cap * 4, 64))
    except Exception:
        # 테이블이 아직 없는 설치본에서도 컨텍스트 조립은 계속돼야 한다.
        _LOG.warning("persona_examples 조회 실패 — 예시 없이 진행한다", exc_info=True)
        return ""
    if not rows:
        return ""

    chunks: List[str] = []
    used = 0
    for example in select_balanced(rows, limit=limit, tag_cap=tag_cap):
        chunk = format_pair(example)
        # 구분용 빈 줄까지 세야 예산이 실제 출력과 맞는다.
        cost = len(chunk) + (2 if chunks else 0)
        if used + cost > char_budget:
            break
        chunks.append(chunk)
        used += cost
    return "\n\n".join(chunks)
