from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from .runtime_config import get_cfg_value

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


def resolve_kg_node_id(project_key: str) -> str | None:
    """project_key(slug)에서 KG node_id를 heuristic으로 찾는다.

    우선순위:
    1. config의 memory.scope.kg_node_map에서 직접 매핑
    2. kg_nodes 테이블에서 하이픈 제거 정규화 후 prefix 매칭
    """
    from .runtime_config import get_cfg_value

    # 1) config 직접 매핑
    mapping = get_cfg_value("memory.scope.kg_node_map", {})
    if isinstance(mapping, dict) and project_key in mapping:
        return mapping[project_key]

    if not project_key:
        return None

    try:
        from .db import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT id FROM kg_nodes WHERE type='project' ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        conn.close()

        # 하이픈·언더스코어를 제거해 정규화한 뒤 prefix 매칭
        def _normalize(s: str) -> str:
            return re.sub(r"[-_]", "", s.lower())

        # project_key에서 digest(마지막 8자 hex) 제거
        key_no_digest = re.sub(r"-[0-9a-f]{8}$", "", project_key)
        key_norm = _normalize(key_no_digest)

        for row in rows:
            node_id: str = row["id"]
            node_norm = _normalize(node_id)
            if node_norm.startswith(key_norm) or key_norm.startswith(node_norm):
                return node_id
    except Exception:
        pass

    return None


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return collapsed or ""
