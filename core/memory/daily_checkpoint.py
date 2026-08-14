"""Append-only daily notes for automatic memory checkpoints."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.config.runtime_config import get_db_root_dir
from core.graph.knowledge import get_kg


def _append_once(path: Path, marker: str, initial: str, block: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else initial
    if marker in text:
        return False
    path.write_text(text.rstrip() + "\n\n" + block.strip() + "\n", encoding="utf-8")
    return True


def _engram_daily_initial(day: str, project_node_id: str) -> str:
    links = f"\nlinks:\n  - {project_node_id}" if project_node_id else ""
    return (
        "---\n"
        f"id: daily-{day}\n"
        f"title: {day} Daily Checkpoints\n"
        "note_type: concept\n"
        "tags:\n"
        "  - daily\n"
        "  - auto-checkpoint\n"
        "summary: 사용자 timezone 기준 자동 메모리 체크포인트 일일노트.\n"
        f"created: {day}\n"
        f"updated: {day}"
        f"{links}\n"
        "---\n\n"
        f"# {day}\n\n"
        "## Engram 자동 체크포인트\n"
    )


def _external_daily_initial() -> str:
    return "---\ntags:\n  - engram\n---\n# To do list\n\n# Engram\n\n## 자동 체크포인트\n"


def _checkpoint_block(
    checkpoint_id: str,
    now: datetime,
    summary: str,
    open_intents: str,
    project_label: str,
    project_node_id: str = "",
    related_path: Path | None = None,
) -> str:
    lines = [
        f"<!-- engram-checkpoint:{checkpoint_id} -->",
        f"### {now.strftime('%H:%M')} — {project_label or 'general'}",
        f"- 요약: {summary}",
    ]
    if open_intents:
        lines.append(f"- 다음 작업: {open_intents}")
    if project_node_id:
        lines.append(f"- 프로젝트: [[{project_node_id}]]")
    if related_path is not None:
        lines.append(f"- 연관 노트: [{related_path.name}]({related_path.as_uri()})")
    return "\n".join(lines)


def append_daily_checkpoint(
    *,
    checkpoint_id: str,
    now: datetime,
    summary: str,
    open_intents: str,
    project_key: str,
    project_node_id: str | None,
    external_daily_dir: str = "",
) -> dict[str, object]:
    day = now.strftime("%Y-%m-%d")
    docs_root = Path(get_db_root_dir()) / "docs"
    engram_path = docs_root / "daily" / f"{day}.md"
    marker = f"engram-checkpoint:{checkpoint_id}"
    project_label = project_node_id or project_key or "general"
    engram_block = _checkpoint_block(
        checkpoint_id,
        now,
        summary,
        open_intents,
        project_label,
        project_node_id=project_node_id or "",
    )
    engram_written = _append_once(
        engram_path,
        marker,
        _engram_daily_initial(day, project_node_id or ""),
        engram_block,
    )

    kg = get_kg()
    kg.sync_file(engram_path, docs_root)
    kg.resolve_links(docs_root, restrict_to_paths={str(engram_path.relative_to(docs_root))})

    external_path: Path | None = None
    external_written = False
    if external_daily_dir:
        external_root = Path(external_daily_dir).expanduser()
        if external_root.exists() or external_root.parent.exists():
            external_path = external_root / f"{day}.md"
            external_block = _checkpoint_block(
                checkpoint_id,
                now,
                summary,
                open_intents,
                project_label,
                project_node_id=project_node_id or "",
                related_path=engram_path,
            )
            external_written = _append_once(
                external_path,
                marker,
                _external_daily_initial(),
                external_block,
            )

    project_written = False
    if project_node_id:
        project_written = kg.append_node_progress_checkpoint(
            project_node_id,
            checkpoint_id=checkpoint_id,
            timestamp=now.strftime("%Y-%m-%d %H:%M"),
            summary=summary,
            open_intents=open_intents,
            daily_node_id=f"daily-{day}",
        )

    return {
        "engram_path": str(engram_path),
        "engram_written": engram_written,
        "external_path": str(external_path) if external_path else "",
        "external_written": external_written,
        "project_written": project_written,
    }
