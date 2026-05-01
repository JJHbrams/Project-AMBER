"""
정체성(self-narrative), 페르소나(persona), 테마 가중치 관리
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from core.storage.db import get_connection
from core.common.sanitizer import sanitize

try:
    import yaml as _yaml
except ModuleNotFoundError:
    _yaml = None

_PERSONA_YAML_REL = "config/persona.yaml"
_USER_PERSONA_YAML_PATH = Path.home() / ".engram" / "persona.user.yaml"


def _resolve_persona_yaml_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent.parent
    return base / _PERSONA_YAML_REL


def _load_yaml_safe(path: Path) -> Dict[str, Any]:
    """YAML 파일을 읽어 dict로 반환. 없거나 파싱 실패 시 {}."""
    _, data = _load_yaml_with_status(path)
    return data


def _load_yaml_with_status(path: Path) -> Tuple[str, Dict[str, Any]]:
    """YAML 파일 로드 결과를 (status, data) 튜플로 반환.
    status: 'missing' | 'invalid' | 'loaded'
    'invalid'는 파일이 존재하지만 파싱 실패. 'missing'은 파일 없음.
    invalid와 missing을 구분하여 호출자가 마지막 상태를 보존할 수 있도록 함.
    """
    if _yaml is None or not path.exists():
        return ("missing", {})
    try:
        with open(path, encoding="utf-8") as f:
            loaded = _yaml.safe_load(f)
        return ("loaded", loaded if isinstance(loaded, dict) else {})
    except Exception:
        return ("invalid", {})


def _is_set(val: Any) -> bool:
    """값이 '설정된' 상태인지 확인. None/빈문자열/빈리스트는 미설정."""
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return bool(val)
    return True  # 숫자 0.0 포함 모든 숫자는 설정된 것으로 취급


def _coerce_persona_field(key: str, val: Any) -> Any:
    """필드 타입에 맞게 값을 강제 변환. 변환 불가 시 None 반환."""
    default = DEFAULT_PERSONA.get(key)
    if default is None:
        return None
    if isinstance(default, (int, float)):
        try:
            return round(max(0.0, min(1.0, float(val))), 2)
        except (TypeError, ValueError):
            return None
    elif isinstance(default, list):
        if isinstance(val, list):
            return [str(i) for i in val if i]
        if isinstance(val, str) and val:
            return [val]
        return None
    elif isinstance(default, str):
        return val if isinstance(val, str) else None
    return val


def _load_persona_yaml() -> Dict[str, Any]:
    """persona 로드 체인: config/persona.yaml → ~/.engram/persona.user.yaml (최우선).
    두 파일 모두 값이 있는 필드만 적용 (옵트인).
    """
    project = _load_yaml_safe(_resolve_persona_yaml_path())
    user = _load_yaml_safe(_USER_PERSONA_YAML_PATH)
    if not project and not user:
        return {}
    # project를 base로, user를 top으로 덮어씌우기
    if project and user:
        return {**project, **{k: v for k, v in user.items() if v is not None and v != [] and v != ""}}
    return project or user


def _apply_yaml_override(base: dict, override: dict) -> dict:
    """override에서 값이 존재하는 필드만 base에 덮어씌운다.
    - str: 비어있지 않으면 override
    - list: 비어있지 않으면 override
    - float/int: None이 아니면 override
    """
    result = {**base}
    for key, val in override.items():
        if key not in result:
            continue
        if isinstance(val, str) and val:
            result[key] = val
        elif isinstance(val, list) and val:
            result[key] = val
        elif isinstance(val, (int, float)) and val is not None:
            result[key] = round(max(0.0, min(1.0, float(val))), 2)
    return result


# ── Persona defaults & merge ──────────────────────────────

DEFAULT_PERSONA = {
    "voice": "",  # 말투 요약 (e.g., "담백하고 직설적")
    "traits": [],  # 성격 키워드 (max 5)
    "quirks": [],  # 고유 습관/버릇 (max 3)
    "values": [],  # 중시하는 가치 (max 3)
    "fewshot": "",  # 말투 예시 대화 (few-shot examples)
    "warmth": 0.5,  # 0.0 차가운 ↔ 1.0 따뜻한
    "formality": 0.5,  # 0.0 반말/캐주얼 ↔ 1.0 격식
    "humor": 0.3,  # 0.0 진지 ↔ 1.0 유머러스
    "directness": 0.5,  # 0.0 완곡 ↔ 1.0 직설
}

_LIST_MAX = {"traits": 5, "quirks": 3, "values": 3}
_BLEND_ALPHA = 0.3  # 새 관찰값 반영 비율 (EMA)


def _merge_persona(current: dict, update: dict) -> dict:
    """현재 persona에 관찰값(update)을 점진적으로 블렌딩.
    - 숫자: EMA (old*0.7 + new*0.3), clamp 0~1
    - 리스트: 새 항목 앞에 추가, 중복 제거, max length 적용
    - 문자열: 비어있지 않으면 교체
    """
    merged = {**DEFAULT_PERSONA, **current}

    for key, new_val in update.items():
        if key not in merged:
            continue
        old_val = merged[key]

        if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
            blended = old_val * (1 - _BLEND_ALPHA) + new_val * _BLEND_ALPHA
            merged[key] = round(max(0.0, min(1.0, blended)), 2)
        elif isinstance(old_val, list) and isinstance(new_val, list):
            max_len = _LIST_MAX.get(key, 5)
            combined = list(dict.fromkeys(new_val + old_val))
            merged[key] = combined[:max_len]
        elif isinstance(old_val, str) and isinstance(new_val, str) and new_val:
            merged[key] = new_val

    return merged


def render_persona(persona: dict) -> str:
    """persona dict → 컨텍스트 주입용 압축 문자열 (40~60 토큰)"""
    p = {**DEFAULT_PERSONA, **persona}
    lines = []
    if p["voice"]:
        lines.append(f"voice: {p['voice']}")
    if p["traits"]:
        lines.append(f"traits: {', '.join(p['traits'])}")
    if p["quirks"]:
        lines.append(f"quirks: {' | '.join(p['quirks'])}")
    if p["values"]:
        lines.append(f"values: {', '.join(p['values'])}")
    dims = [f"{d}:{p[d]}" for d in ("warmth", "formality", "humor", "directness")]
    lines.append(" ".join(dims))
    if p.get("fewshot"):
        lines.append(f"[examples]\n{p['fewshot']}")
    return "\n".join(lines)


# ── Narrative ──────────────────────────────────────────────


def get_identity() -> Dict:
    conn = get_connection()
    row = conn.execute("SELECT * FROM identity WHERE id=1").fetchone()
    conn.close()
    identity = dict(row) if row else {}

    yaml_persona = _load_persona_yaml()
    if yaml_persona and isinstance(yaml_persona.get("name"), str) and yaml_persona["name"]:
        identity["name"] = yaml_persona["name"]

    return identity


def update_narrative(new_narrative: str, new_name: str = None):
    new_narrative = sanitize(new_narrative, max_length=1000)
    if new_name:
        new_name = sanitize(new_name, max_length=50)
    conn = get_connection()
    with conn:
        if new_name:
            conn.execute(
                """UPDATE identity SET narrative=?, name=?,
                   updated_at=datetime('now','localtime') WHERE id=1""",
                (new_narrative, new_name),
            )
        else:
            conn.execute(
                """UPDATE identity SET narrative=?,
                   updated_at=datetime('now','localtime') WHERE id=1""",
                (new_narrative,),
            )
    conn.close()


# ── Persona ───────────────────────────────────────────────


def get_persona() -> Dict:
    """현재 persona를 반환. 필드별 우선순위:
    1. user.yaml에 값 있는 필드 (pinned) — user.yaml 파싱 실패 시 이 단계 건너뜀
    2. DB 진화값
    3. project.yaml 기본값
    4. DEFAULT_PERSONA
    """
    project_status, project_yaml = _load_yaml_with_status(_resolve_persona_yaml_path())
    user_status, user_yaml = _load_yaml_with_status(_USER_PERSONA_YAML_PATH)

    conn = get_connection()
    row = conn.execute("SELECT persona FROM identity WHERE id=1").fetchone()
    conn.close()
    db_persona: Dict[str, Any] = {}
    if row and row["persona"]:
        try:
            db_persona = json.loads(row["persona"]) or {}
        except (json.JSONDecodeError, TypeError):
            pass

    result = {}
    for key, default_val in DEFAULT_PERSONA.items():
        # 1. user.yaml pin (invalid 상태면 건너뜀 — 파싱 실패로 pinned 필드 풀리지 않도록)
        if user_status == "loaded":
            user_val = user_yaml.get(key)
            if _is_set(user_val):
                coerced = _coerce_persona_field(key, user_val)
                if coerced is not None:
                    result[key] = coerced
                    continue
        # 2. DB
        db_val = db_persona.get(key)
        if _is_set(db_val):
            coerced = _coerce_persona_field(key, db_val)
            if coerced is not None:
                result[key] = coerced
                continue
        # 3. project.yaml
        if project_status == "loaded":
            proj_val = project_yaml.get(key)
            if _is_set(proj_val):
                coerced = _coerce_persona_field(key, proj_val)
                if coerced is not None:
                    result[key] = coerced
                    continue
        # 4. DEFAULT
        result[key] = default_val

    return result


def update_persona(observations: dict) -> Dict:
    """관찰값을 현재 persona에 블렌딩하여 저장. 변경 후 persona 반환.
    - user.yaml에 값이 있는 필드(pinned)는 EMA 블렌딩 대상에서 제외
    - DB에는 pinned 필드를 건드리지 않음 (user.yaml 사라질 때 복귀용으로 DB값 보존)
    - user.yaml 파싱 실패 시 모든 필드를 pinned로 취급하지 않음
    - DB가 미초기화 상태면 project.yaml로 자동 seed 후 진행
    """
    if not is_persona_initialized():
        seed_persona("project_yaml")

    user_status, user_yaml = _load_yaml_with_status(_USER_PERSONA_YAML_PATH)
    pinned: set = set()
    if user_status == "loaded":
        pinned = {
            k for k, v in user_yaml.items()
            if k in DEFAULT_PERSONA and _is_set(v) and _coerce_persona_field(k, v) is not None
        }

    project_status, project_yaml = _load_yaml_with_status(_resolve_persona_yaml_path())

    conn = get_connection()
    row = conn.execute("SELECT persona FROM identity WHERE id=1").fetchone()
    conn.close()
    db_persona: Dict[str, Any] = {}
    if row and row["persona"]:
        try:
            db_persona = json.loads(row["persona"]) or {}
        except (json.JSONDecodeError, TypeError):
            pass

    # DB 기반 base (user.yaml 값 제외)
    base: Dict[str, Any] = {}
    for key, default_val in DEFAULT_PERSONA.items():
        base[key] = default_val
        if project_status == "loaded" and _is_set(project_yaml.get(key)):
            coerced = _coerce_persona_field(key, project_yaml[key])
            if coerced is not None:
                base[key] = coerced
        db_val = db_persona.get(key)
        if _is_set(db_val):
            coerced = _coerce_persona_field(key, db_val)
            if coerced is not None:
                base[key] = coerced

    # pinned 필드 제외하고 EMA 블렌딩
    filtered_obs = {k: v for k, v in observations.items() if k not in pinned}
    merged = _merge_persona(base, filtered_obs)

    # DB 업데이트: pinned 필드는 기존 DB값 유지 (user.yaml 제거 시 복귀용)
    db_update = {**db_persona}
    for key, val in merged.items():
        if key not in pinned:
            db_update[key] = val

    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE identity SET persona=?, updated_at=datetime('now','localtime') WHERE id=1",
            (json.dumps(db_update, ensure_ascii=False),),
        )
    conn.close()
    return get_persona()


def set_persona_baseline(fields: dict) -> Dict:
    """설정 UI 등에서 지정한 persona 필드를 DB baseline으로 즉시 반영한다.

    user persona YAML의 pinned 필드보다 우선하지는 않으며,
    get_persona()의 우선순위 규칙(user > db > project > default)을 그대로 따른다.
    """
    if not isinstance(fields, dict) or not fields:
        return get_persona()

    if not is_persona_initialized():
        seed_persona("project_yaml")

    conn = get_connection()
    row = conn.execute("SELECT persona FROM identity WHERE id=1").fetchone()
    db_persona: Dict[str, Any] = {}
    if row and row["persona"]:
        try:
            db_persona = json.loads(row["persona"]) or {}
        except (json.JSONDecodeError, TypeError):
            db_persona = {}

    changed = False
    for key, raw_val in fields.items():
        if key not in DEFAULT_PERSONA:
            continue
        coerced = _coerce_persona_field(key, raw_val)
        if coerced is None:
            continue
        if db_persona.get(key) != coerced:
            db_persona[key] = coerced
            changed = True

    if changed:
        with conn:
            conn.execute(
                "UPDATE identity SET persona=?, updated_at=datetime('now','localtime') WHERE id=1",
                (json.dumps(db_persona, ensure_ascii=False),),
            )
    conn.close()
    return get_persona()


def is_persona_initialized() -> bool:
    """DB persona가 초기화되어 있는지 확인. {} 또는 null이면 미초기화."""
    conn = get_connection()
    row = conn.execute("SELECT persona FROM identity WHERE id=1").fetchone()
    conn.close()
    if not row or not row["persona"]:
        return False
    try:
        p = json.loads(row["persona"])
        return bool(p)
    except (json.JSONDecodeError, TypeError):
        return False


def seed_persona(source: str = "project_yaml") -> dict:
    """최초 실행 시 DB persona를 초기화.
    source: 'user_yaml' | 'project_yaml' | 'default'
    - 'user_yaml': user.yaml 필드 우선, 누락 필드는 project.yaml로 채움. user.yaml 없으면 project.yaml로 fallback.
    - 'project_yaml': config/persona.yaml 값으로 초기화
    - 'default': DEFAULT_PERSONA 값으로 초기화
    atomic compare-and-set: 이미 초기화된 경우 현재 persona 반환 (덮어쓰지 않음).
    """
    project_status, project_yaml = _load_yaml_with_status(_resolve_persona_yaml_path())
    user_status, user_yaml = _load_yaml_with_status(_USER_PERSONA_YAML_PATH)

    if source == "user_yaml" and user_status == "loaded":
        base: Dict[str, Any] = {}
        for key, default_val in DEFAULT_PERSONA.items():
            base[key] = default_val
            if project_status == "loaded" and _is_set(project_yaml.get(key)):
                coerced = _coerce_persona_field(key, project_yaml[key])
                if coerced is not None:
                    base[key] = coerced
            if _is_set(user_yaml.get(key)):
                coerced = _coerce_persona_field(key, user_yaml[key])
                if coerced is not None:
                    base[key] = coerced
    elif source != "default":
        base = {}
        for key, default_val in DEFAULT_PERSONA.items():
            base[key] = default_val
            if project_status == "loaded" and _is_set(project_yaml.get(key)):
                coerced = _coerce_persona_field(key, project_yaml[key])
                if coerced is not None:
                    base[key] = coerced
    else:
        base = {**DEFAULT_PERSONA}

    conn = get_connection()
    with conn:
        cursor = conn.execute(
            """UPDATE identity SET persona=?, updated_at=datetime('now','localtime')
               WHERE id=1 AND (persona IS NULL OR persona='' OR persona='{}')""",
            (json.dumps(base, ensure_ascii=False),),
        )
    affected = cursor.rowcount
    conn.close()

    if affected == 0:
        return get_persona()
    return base


def get_persona_status() -> dict:
    """persona 초기화 상태 및 user.yaml 상태를 반환."""
    initialized = is_persona_initialized()
    user_status, _ = _load_yaml_with_status(_USER_PERSONA_YAML_PATH)
    return {
        "initialized": initialized,
        "user_yaml_status": user_status,  # 'missing' | 'invalid' | 'loaded'
        "user_yaml_path": str(_USER_PERSONA_YAML_PATH),
    }


# ── Themes ─────────────────────────────────────────────────


def get_themes(top_n: int = 10) -> List[Tuple[str, float]]:
    conn = get_connection()
    rows = conn.execute("SELECT name, weight FROM themes ORDER BY weight DESC LIMIT ?", (top_n,)).fetchall()
    conn.close()
    return [(r["name"], r["weight"]) for r in rows]


def update_themes_from_text(text: str):
    """텍스트에서 주제어를 추출해 가중치 누적"""
    keywords = _extract_themes(text)
    if not keywords:
        return
    conn = get_connection()
    with conn:
        for kw in keywords:
            conn.execute(
                """INSERT INTO themes (name, weight, last_seen)
                   VALUES (?, 1.0, datetime('now','localtime'))
                   ON CONFLICT(name) DO UPDATE SET
                       weight = weight + 0.5,
                       last_seen = datetime('now','localtime')""",
                (kw,),
            )
    conn.close()


def decay_themes(factor: float = 0.95):
    """세션 종료 시 오래된 테마 자연 감쇠"""
    conn = get_connection()
    with conn:
        conn.execute("UPDATE themes SET weight = weight * ?", (factor,))
        conn.execute("DELETE FROM themes WHERE weight < 0.1")
    conn.close()


def _extract_themes(text: str) -> List[str]:
    # 한글 명사형 2~6자 단어 추출 (단순 규칙 기반)
    candidates = re.findall(r"[가-힣]{2,6}", text)
    stop = {
        "나는",
        "그것",
        "하지만",
        "그리고",
        "그래서",
        "때문",
        "것이",
        "있다",
        "없다",
        "한다",
        "된다",
        "하면",
        "이라",
        "으로",
        "에서",
        "이다",
        "했다",
        "했습",
        "니다",
        "습니",
        "하는",
        "있는",
        "없는",
    }
    return [w for w in candidates if w not in stop]

