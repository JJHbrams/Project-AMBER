"""Runtime config loader.

Load order:
1. Built-in defaults
2. config/config.yaml (project default)
3. ~/.engram/runtime.user.yaml (legacy user override)
4. ~/.engram/user.config.yaml (preferred user override)
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


_DEFAULT_REL = "config/config.yaml"
_LEGACY_USER_CONFIG_PATH = Path.home() / ".engram" / "runtime.user.yaml"
_USER_CONFIG_PATH = Path.home() / ".engram" / "user.config.yaml"

_DEFAULT_CFG = {
    "db": {
        "root_dir": "D:/intel_engram",
    },
    "memory": {
        "scope": {
            "default_main": "default:main",
            "default_fallback": "default:main",
            "default_global": "global:main",
            "project_prefix": "project:",
            "discord_prefix": "discord:",
        },
        "short_term": {
            "limit_turns": 8,
            "within_minutes": 120,
            "max_turn_chars": 80,
        },
        "working": {
            "ttl_hours": 48,
            "max_compact_length": 900,
            "prompt_summary_max_chars": 240,
            "store_summary_max_chars": 1200,
            "store_open_intents_max_chars": 600,
            "user_clip_chars": 120,
            "assistant_clip_chars": 160,
        },
        "long_term": {
            "search_limit": 2,
            "item_max_chars": 100,
        },
    },
    "copilot": {
        "model": "claude-sonnet-4.6",
        "allow_all_tools": True,
    },
}

_USER_TEMPLATE = """# User runtime overrides for Engram.
# Only put keys you want to change.

# db:
#   root_dir: "D:/intel_engram"
#
# memory:
#   scope:
#     default_main: "default:main"
#     default_global: "global:main"
#     project_prefix: "project:"
#     discord_prefix: "discord:"
#   short_term:
#     limit_turns: 8
#     within_minutes: 120
#   working:
#     ttl_hours: 48
#
# copilot:
#   model: "claude-sonnet-4.6"
#   allow_all_tools: true
"""

_CACHED_CFG: dict[str, Any] | None = None
_CACHED_SIG: tuple[int, int] | None = None


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def _get_bundle_dir() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent


def resolve_runtime_path(rel: str = _DEFAULT_REL) -> Path:
    external = _get_base_dir() / rel
    if external.exists():
        return external
    bundle = _get_bundle_dir() / rel
    if bundle.exists():
        return bundle
    return external


def _safe_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _ensure_user_config_file():
    if _USER_CONFIG_PATH.exists():
        return
    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_CONFIG_PATH.write_text(_USER_TEMPLATE, encoding="utf-8")


def load_runtime_cfg(force_reload: bool = False) -> dict[str, Any]:
    global _CACHED_CFG, _CACHED_SIG

    default_path = resolve_runtime_path(_DEFAULT_REL)
    _ensure_user_config_file()

    sig = (
        _safe_mtime(default_path),
        _safe_mtime(_LEGACY_USER_CONFIG_PATH),
        _safe_mtime(_USER_CONFIG_PATH),
    )
    if not force_reload and _CACHED_CFG is not None and _CACHED_SIG == sig:
        return copy.deepcopy(_CACHED_CFG)

    cfg = copy.deepcopy(_DEFAULT_CFG)
    cfg = _deep_merge(cfg, _read_yaml(default_path))
    cfg = _deep_merge(cfg, _read_yaml(_LEGACY_USER_CONFIG_PATH))
    cfg = _deep_merge(cfg, _read_yaml(_USER_CONFIG_PATH))

    _CACHED_CFG = cfg
    _CACHED_SIG = sig
    return copy.deepcopy(cfg)


def get_cfg_value(path: str, default: Any = None) -> Any:
    current: Any = load_runtime_cfg()
    for token in path.split("."):
        if not isinstance(current, dict):
            return default
        if token not in current:
            return default
        current = current[token]
    return current


def get_default_main_scope_key() -> str:
    default = _DEFAULT_CFG["memory"]["scope"]["default_main"]
    return str(get_cfg_value("memory.scope.default_main", default))


def get_default_fallback_scope_key() -> str:
    default = _DEFAULT_CFG["memory"]["scope"]["default_fallback"]
    return str(get_cfg_value("memory.scope.default_fallback", default))


def get_discord_scope_prefix() -> str:
    default = _DEFAULT_CFG["memory"]["scope"]["discord_prefix"]
    return str(get_cfg_value("memory.scope.discord_prefix", default))


def get_db_root_dir() -> str:
    default = _DEFAULT_CFG["db"]["root_dir"]
    for path in (_USER_CONFIG_PATH, _LEGACY_USER_CONFIG_PATH):
        loaded = _read_yaml(path)
        if not isinstance(loaded, dict):
            continue
        db_cfg = loaded.get("db")
        if not isinstance(db_cfg, dict):
            continue
        configured = str(db_cfg.get("root_dir", "")).strip()
        if configured:
            return configured
    env_override = os.environ.get("ENGRAM_DB_DIR", "").strip()
    if env_override:
        return env_override
    project_cfg = _read_yaml(resolve_runtime_path(_DEFAULT_REL))
    if isinstance(project_cfg, dict):
        db_cfg = project_cfg.get("db")
        if isinstance(db_cfg, dict):
            configured = str(db_cfg.get("root_dir", "")).strip()
            if configured:
                return configured
    return default


def get_copilot_model() -> str:
    default = _DEFAULT_CFG["copilot"]["model"]
    return str(get_cfg_value("copilot.model", default))


def get_copilot_allow_all_tools() -> bool:
    default = bool(_DEFAULT_CFG["copilot"]["allow_all_tools"])
    return bool(get_cfg_value("copilot.allow_all_tools", default))


def get_watch_workspaces() -> list[str]:
    """user.config.yaml의 watch_workspaces 반환.

    각 항목은 git 프로젝트들이 모여있는 상위 디렉토리 경로.
    watcher가 하위 git repo를 자동 감지하여 개념 파일 변경을 wiki에 반영한다.
    """
    raw = get_cfg_value("watch_workspaces", [])
    if not isinstance(raw, list):
        return []
    return [str(p).strip() for p in raw if str(p).strip()]


_DEFAULT_CONCEPTUAL_FILES = [
    "README.md",
    "architecture.md",
    "docs/architecture.md",
]


def get_watch_conceptual_files() -> list[str]:
    """wiki 자동 동기화 대상 파일 패턴 목록 (프로젝트 루트 기준 glob)."""
    raw = get_cfg_value("watch_conceptual_files", _DEFAULT_CONCEPTUAL_FILES)
    if not isinstance(raw, list):
        return list(_DEFAULT_CONCEPTUAL_FILES)
    return [str(p).strip() for p in raw if str(p).strip()]
