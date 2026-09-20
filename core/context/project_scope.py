from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from core.config.runtime_config import get_cfg_value
from core.storage.db import get_connection

_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
)


def get_global_scope_key() -> str:
    default = "global:main"
    return str(get_cfg_value("memory.scope.default_global", default)).strip() or default


def get_project_scope_prefix() -> str:
    default = "project:"
    return str(get_cfg_value("memory.scope.project_prefix", default)).strip() or default


def resolve_scope_key(
    scope_key: Optional[str] = None,
    *,
    project_key: Optional[str] = None,
    cwd: Optional[str] = None,
) -> str:
    explicit_scope = (scope_key or "").strip()
    if explicit_scope:
        return explicit_scope

    resolved_project_key = resolve_project_key(project_key=project_key, cwd=cwd)
    if resolved_project_key:
        return f"{get_project_scope_prefix()}{resolved_project_key}"

    return get_global_scope_key()


def resolve_project_key(project_key: Optional[str] = None, cwd: Optional[str] = None) -> str:
    explicit_project_key = _slugify(project_key or "")
    if explicit_project_key:
        return explicit_project_key

    project_root = detect_project_root(cwd=cwd)
    if project_root is None:
        return ""

    return _project_key_from_path(project_root)


def cwd_is_foreign(cwd: Optional[str]) -> bool:
    """클라이언트가 준 cwd 가 이 서버 파일시스템에 실재하지 않는지 판정한다.

    원격(SSH 리버스 터널) 클라이언트는 자기 쪽 경로를 보내는데 그 경로는 서버에 없다.
    그러면 detect_project_root 가 None 을 반환하고 조용히 global 스코프로 폴백해
    프로젝트별 기억이 한 바구니에 섞인다. 호출부에서 이 조건을 감지해 경고한다.
    """
    if not cwd:
        return False
    try:
        return not Path(cwd).exists()
    except OSError:
        return True


def detect_project_root(cwd: Optional[str] = None) -> Optional[Path]:
    raw_path = Path(cwd or os.getcwd())
    start_path = raw_path if raw_path.is_dir() else raw_path.parent

    try:
        resolved = start_path.resolve()
    except OSError:
        return None

    for candidate in (resolved, *resolved.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate

    return None


def _project_key_from_path(path: Path) -> str:
    normalized = str(path).lower()
    slug = _slugify(path.name)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def resolve_project_tag(cwd: Optional[str] = None) -> str:
    """조회용 프로젝트 태그. **스코프가 아니다** — 저장 위치를 바꾸지 않는다.

    로컬 경로면 resolve_project_key 와 같은 값을 쓴다. 원격(리버스 터널) 경로는
    이 파일시스템에 없어서 마커 탐색이 실패하는데, 그렇다고 버리면 원격 작업이
    전부 미분류로 쌓인다. 경로 문자열에서 이름만 뽑아 remote: 접두사로 묶는다 —
    로컬 키와 형식이 달라 섞이지 않고, 적어도 프로젝트별로 모이기는 한다.

    판정 실패는 빈 문자열이다. 추정해서 채우면 틀린 태그가 되고, 틀린 태그는
    빈 태그보다 나쁘다 — 없는 건 찾으면 되지만 틀린 건 찾았다고 착각하게 한다.
    """
    raw = (cwd or "").strip()
    if not raw:
        return ""
    if cwd_is_foreign(raw):
        name = _slugify(Path(raw.replace(chr(92), "/")).name)
        return f"remote:{name}" if name else ""
    return resolve_project_key(cwd=raw)


_OVERVIEW_STEMS = ("overview", "index")


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_]", "", (value or "").lower())


def _project_dir_aliases(directory: str) -> set[str]:
    """위키 프로젝트 디렉터리가 가리킬 수 있는 이름들.

    ``001_TruviewCADMOM`` 처럼 정렬용 숫자 접두사를 붙이는 관습이 있어서
    디렉터리명 그대로와 접두사를 뗀 이름 둘 다 후보다.
    """
    names = {directory}
    stripped = re.sub(r"^\d+[_-]", "", directory)
    if stripped:
        names.add(stripped)
    return {n for n in (_normalize_name(name) for name in names) if n}


def _overview_candidates() -> dict[str, set[str]]:
    """프로젝트 이름 -> 그 프로젝트의 overview 노드 id 집합.

    위키는 프로젝트마다 디렉터리 하나를 두고 그 **바로 아래**에 대표 노트를 둔다.
    파일명 관습은 갈린다 — ``overview.md``, ``index.md``, ``<프로젝트>-overview.md``,
    ``<프로젝트>.md`` 가 모두 쓰인다. 하위 디렉터리(``report/``, ``dev/``)의 노트는
    대표 노트가 아니다.

    깊이 제한이 핵심이다. ``projects/**`` 아래 노트는 전부 ``type='project'`` 로
    저장되기 때문에, 그 제한이 없으면 ``000_Project_Engram/report/
    session-agent-orchestration.md`` 같은 하위 보고서가 같은 이름의 프로젝트
    키에 먼저 걸린다(2026-09-18 실측).
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, path FROM kg_nodes WHERE type='project'").fetchall()
    finally:
        conn.close()

    candidates: dict[str, set[str]] = {}
    for row in rows:
        node_id = str(row["id"] or "").strip()
        parts = str(row["path"] or "").replace("\\", "/").split("/")
        # projects/<디렉터리>/<파일>.md 만 대표 노트 후보다.
        if len(parts) != 3 or parts[0] != "projects":
            continue
        directory = parts[1]
        stem = re.sub(r"\.md$", "", parts[2], flags=re.IGNORECASE)
        aliases = _project_dir_aliases(directory)
        stem_norm = _normalize_name(stem)
        is_overview = (
            stem.lower() in _OVERVIEW_STEMS
            or stem_norm in aliases
            or any(stem_norm == f"{alias}overview" for alias in aliases)
        )
        if not is_overview or not node_id:
            continue
        for alias in aliases:
            candidates.setdefault(alias, set()).add(node_id)
        # id 자체로도 찾을 수 있게 한다 — 디렉터리명과 다른 경우가 있다.
        for name in (node_id, re.sub(r"-overview$", "", node_id)):
            normalized = _normalize_name(name)
            if normalized:
                candidates.setdefault(normalized, set()).add(node_id)
    return candidates


def resolve_kg_node_id(project_key: str) -> str | None:
    """project_key(slug)에 대응하는 프로젝트 overview 노드 id. 없으면 None.

    우선순위:
    1. config의 memory.scope.kg_node_map에서 직접 매핑
    2. 위키 프로젝트 디렉터리의 대표(overview) 노트 중 이름이 정확히 일치하는 것

    추측하지 않는다. 자동 체크포인트는 여기서 나온 노드의 ``## Progress`` 를
    실제로 덮어쓰므로, 틀린 매칭은 무관한 위키 문서를 오염시킨다. 후보가 둘 이상
    이면 고르지 않고 None 을 돌려준다.

    이전 구현은 노드 id 정확 일치만 봤다. 그래서 (a) 대표 노트를
    ``<프로젝트>-overview.md`` 로 만든 신규 프로젝트는 링크가 아예 안 붙고
    (``claude-image-forge``), (b) 하위 보고서가 프로젝트 키와 이름이 같으면
    그쪽이 걸렸다 (``session-agent-orchestration`` → ``000_Project_Engram/report/``).
    2026-09-18 실측.

    매칭이 없으면 호출부는 project_key 자체를 기록한다. 링크가 없는 것은 정보가
    덜 남는 것이지만, 틀린 링크는 다른 문서를 망가뜨린다.
    """
    mapping = get_cfg_value("memory.scope.kg_node_map", {})
    key_no_digest = re.sub(r"-[0-9a-f]{8}$", "", project_key or "")
    if isinstance(mapping, dict):
        if project_key in mapping:
            return mapping[project_key]
        if key_no_digest in mapping:
            return mapping[key_no_digest]

    key_norm = _normalize_name(key_no_digest)
    if not key_norm:
        return None

    try:
        matches = _overview_candidates().get(key_norm, set())
    except Exception:
        return None
    return next(iter(matches)) if len(matches) == 1 else None


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return collapsed or ""
