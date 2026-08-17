"""Frozen installer bootstrap for DB, wiki starter files, and directives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path


WIKI_DIRS = (
    "_inbox",
    "_templates",
    "concepts",
    "daily",
    "guides",
    "moc",
    "notes",
    "people",
    "projects",
    "protocols",
    "references",
    "research",
    "research/llm",
    "research/knowledge-systems",
    "research/agent",
    "research/cost",
    "tools",
)

TEMPLATE_TARGETS = (
    ("_home.md", "moc/000-HOME.md"),
    ("_wiki-guide.md", "guides/wiki-guide.md"),
    ("concept.md", "_templates/concept.md"),
    ("project.md", "_templates/project.md"),
    ("research.md", "_templates/research.md"),
    ("person.md", "_templates/person.md"),
    ("protocols/_protocol-wiki-management-guide.md", "protocols/wiki-management-guide.md"),
    ("protocols/_protocol-git-branch-guide.md", "protocols/git-branch-guide.md"),
    ("protocols/_protocol-agent-collaboration-guide.md", "protocols/agent-collaboration-guide.md"),
    ("protocols/_protocol-wiki-reminder-guide.md", "protocols/wiki-reminder-guide.md"),
    ("protocols/_protocol-activity-log-guide.md", "protocols/activity-log-guide.md"),
    ("protocols/_protocol-narrative-update-guide.md", "protocols/narrative-update-guide.md"),
    ("protocols/_protocol-reflection-trigger-guide.md", "protocols/reflection-trigger-guide.md"),
)

INSTALL_MANAGED_SOURCE = "install-managed"
MANUAL_ROOT_RELATIVE = Path("guides") / "engram-manual"
MANUAL_MANIFEST_RELATIVE = Path("manual") / "manifest.json"
MANUAL_STATE_NAME = ".install-manifest.json"
LEGACY_MANUAL_PATHS = {
    "index.md",
    "tutorial.md",
    "overlay-settings.md",
    "skills.md",
    "mcp-tools.md",
    "troubleshooting.md",
}
OBSOLETE_INSTALL_DIRECTIVE_KEYS = {
    "wiki-governance-trigger",
    "wiki-management",
    "wiki-reminder-on-task",
    "git-branch-policy",
    "activity-log",
    "narrative-update-guard",
    "reflection-trigger",
}


def _replace_placeholder(value, replacement: str):
    if isinstance(value, str):
        return value.replace("__VAULT_DIR__", replacement)
    if isinstance(value, list):
        return [_replace_placeholder(item, replacement) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholder(item, replacement) for key, item in value.items()}
    return value


def _safe_manual_relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.name != value.split("/")[-1]:
        raise ValueError(f"unsafe manual manifest path: {value!r}")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"unsafe manual manifest path: {value!r}")
    return path


def _load_manual_manifest(templates_dir: Path) -> tuple[dict, set[Path]]:
    manifest_path = templates_dir / MANUAL_MANIFEST_RELATIVE
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manual manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("manual manifest must contain a files list")
    files = {_safe_manual_relative_path(str(item)) for item in manifest["files"]}
    if not files:
        raise ValueError("manual manifest files must not be empty")
    for relative in files:
        source = templates_dir / "manual" / relative
        if not source.is_file():
            raise FileNotFoundError(f"manual page missing: {source}")
    return manifest, files


def _read_previous_manual_paths(state_path: Path) -> set[Path] | None:
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    files = state.get("managed_files") if isinstance(state, dict) else None
    if not isinstance(files, list):
        raise ValueError("installed manual state must contain a managed_files list")
    return {_safe_manual_relative_path(str(item)) for item in files}


def _install_managed_manual(docs_dir: Path, templates_dir: Path) -> dict[str, int]:
    manifest, current_paths = _load_manual_manifest(templates_dir)
    manual_root = docs_dir / MANUAL_ROOT_RELATIVE
    manual_root.mkdir(parents=True, exist_ok=True)
    state_path = manual_root / MANUAL_STATE_NAME
    previous_paths = _read_previous_manual_paths(state_path)
    legacy_paths = manifest.get("legacy_managed_paths", sorted(LEGACY_MANUAL_PATHS))
    if not isinstance(legacy_paths, list):
        raise ValueError("manual manifest legacy_managed_paths must be a list")
    removable_paths = previous_paths if previous_paths is not None else {
        _safe_manual_relative_path(str(path)) for path in legacy_paths
    }

    removed = 0
    for relative in removable_paths - current_paths:
        target = manual_root / relative
        if target.is_file():
            target.unlink()
            removed += 1

    created = 0
    updated = 0
    hashes: dict[str, str] = {}
    for relative in sorted(current_paths):
        source = templates_dir / "manual" / relative
        target = manual_root / relative
        content = source.read_text(encoding="utf-8").replace("__DATE__", date.today().isoformat())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            updated += 1
        else:
            created += 1
        target.write_text(content, encoding="utf-8")
        hashes[relative.as_posix()] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    state = {
        "schema_version": 1,
        "manual_version": manifest.get("manual_version", "unknown"),
        "managed_files": sorted(path.as_posix() for path in current_paths),
        "content_hashes": hashes,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manual_files_created": created, "manual_files_updated": updated, "manual_files_removed": removed}


def _seed_directives(db_dir: Path, directives: list[dict]) -> tuple[int, int, int]:
    from core.storage.db import get_connection
    from core.context.directive_policy import coerce_directive_record

    seeded = 0
    updated = 0
    removed = 0
    conn = get_connection(db_dir)
    try:
        with conn:
            for directive in directives:
                materialized = _replace_placeholder(directive, str(db_dir))
                record = coerce_directive_record(
                    {
                        **materialized,
                        "source": INSTALL_MANAGED_SOURCE,
                    }
                )
                existing = conn.execute(
                    "SELECT source, created_at, updated_at FROM directives WHERE key = ?",
                    (record["key"],),
                ).fetchone()
                values = (
                    record["content"],
                    INSTALL_MANAGED_SOURCE,
                    record.get("scope", "all"),
                    record.get("priority", 0),
                    record.get("trigger_type", "always"),
                    record.get("enforcement_level", "advisory"),
                    json.dumps(record.get("trigger_data", {}), ensure_ascii=False, sort_keys=True),
                    record.get("workflow_skill_id", ""),
                    record.get("guard_id", ""),
                    json.dumps(record.get("legacy_migration_markers", []), ensure_ascii=False, sort_keys=True),
                )
                if existing is None:
                    conn.execute(
                        "INSERT INTO directives "
                        "("
                        "key, content, source, scope, priority, trigger_type, "
                        "enforcement_level, trigger_data, workflow_skill_id, guard_id, "
                        "legacy_migration_markers"
                        ") "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record["key"], *values),
                    )
                    seeded += 1
                    continue

                source = str(existing["source"])
                unmodified_legacy = (
                    source == "install"
                    and existing["created_at"] == existing["updated_at"]
                )
                if source == INSTALL_MANAGED_SOURCE or unmodified_legacy:
                    conn.execute(
                        "UPDATE directives SET content = ?, source = ?, scope = ?, "
                        "priority = ?, trigger_type = ?, enforcement_level = ?, "
                        "trigger_data = ?, workflow_skill_id = ?, guard_id = ?, "
                        "legacy_migration_markers = ?, active = 1, "
                        "updated_at = datetime('now','localtime') WHERE key = ?",
                        (*values, record["key"]),
                    )
                    updated += 1

            placeholders = ",".join("?" for _ in OBSOLETE_INSTALL_DIRECTIVE_KEYS)
            cursor = conn.execute(
                f"DELETE FROM directives WHERE key IN ({placeholders}) "
                "AND (source = ? OR (source = 'install' AND created_at = updated_at))",
                (*sorted(OBSOLETE_INSTALL_DIRECTIVE_KEYS), INSTALL_MANAGED_SOURCE),
            )
            removed = max(cursor.rowcount, 0)
    finally:
        conn.close()
    return seeded, updated, removed


def bootstrap_install(db_dir: Path, templates_dir: Path) -> dict[str, int]:
    os.environ["ENGRAM_DB_DIR"] = str(db_dir)

    from core.storage.db import initialize_db

    initialize_db(db_dir)
    docs_dir = db_dir / "docs"
    for relative in WIKI_DIRS:
        (docs_dir / relative).mkdir(parents=True, exist_ok=True)

    created = 0
    today = date.today().isoformat()
    for source_relative, target_relative in TEMPLATE_TARGETS:
        source = templates_dir / source_relative
        target = docs_dir / target_relative
        if target.exists():
            continue
        if not source.is_file():
            raise FileNotFoundError(f"installer template missing: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8").replace("__DATE__", today)
        target.write_text(content, encoding="utf-8")
        created += 1

    manual_result = _install_managed_manual(docs_dir, templates_dir)

    directives_path = templates_dir / "directives.json"
    if not directives_path.is_file():
        raise FileNotFoundError(f"directives template missing: {directives_path}")
    directives = json.loads(directives_path.read_text(encoding="utf-8"))

    seeded, updated, removed = _seed_directives(db_dir, directives)

    return {
        "wiki_files_created": created,
        **manual_result,
        "directives_seeded": seeded,
        "directives_updated": updated,
        "directives_removed": removed,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--templates-dir", required=True)
    args = parser.parse_args(argv)
    result = bootstrap_install(Path(args.db_dir), Path(args.templates_dir))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
