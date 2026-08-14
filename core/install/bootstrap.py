"""Frozen installer bootstrap for DB, wiki starter files, and directives."""

from __future__ import annotations

import argparse
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
OBSOLETE_INSTALL_DIRECTIVE_KEYS = {
    "wiki-governance-trigger",
    "wiki-management",
    "wiki-reminder-on-task",
    "git-branch-policy",
    "activity-log",
    "narrative-update-guard",
    "reflection-trigger",
}


def _seed_directives(db_dir: Path, directives: list[dict]) -> tuple[int, int, int]:
    from core.storage.db import get_connection

    seeded = 0
    updated = 0
    removed = 0
    conn = get_connection(db_dir)
    try:
        with conn:
            for directive in directives:
                content = directive["content"].replace("__VAULT_DIR__", str(db_dir))
                existing = conn.execute(
                    "SELECT source, created_at, updated_at FROM directives WHERE key = ?",
                    (directive["key"],),
                ).fetchone()
                values = (
                    content,
                    INSTALL_MANAGED_SOURCE,
                    directive.get("scope", "all"),
                    directive.get("priority", 0),
                    directive.get("trigger_type", "always"),
                )
                if existing is None:
                    conn.execute(
                        "INSERT INTO directives "
                        "(key, content, source, scope, priority, trigger_type) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (directive["key"], *values),
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
                        "priority = ?, trigger_type = ?, active = 1, "
                        "updated_at = datetime('now','localtime') WHERE key = ?",
                        (*values, directive["key"]),
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

    directives_path = templates_dir / "directives.json"
    if not directives_path.is_file():
        raise FileNotFoundError(f"directives template missing: {directives_path}")
    directives = json.loads(directives_path.read_text(encoding="utf-8"))

    seeded, updated, removed = _seed_directives(db_dir, directives)

    return {
        "wiki_files_created": created,
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
