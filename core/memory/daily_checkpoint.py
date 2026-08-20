"""Append-only daily notes for automatic memory checkpoints."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from core.config.runtime_config import get_cfg_value, get_db_root_dir
from core.graph.knowledge import get_kg


_EXTERNAL_PATH_LOCKS: dict[str, threading.Lock] = {}
_EXTERNAL_PATH_LOCKS_GUARD = threading.Lock()
_V2_BLOCK_RE = re.compile(
    r"<!-- engram-external-project-v2:(?P<identity>[^\r\n>]+) -->\r?\n"
    r"## (?P<title>[^\r\n]+)\r?\n"
    r"<!-- engram-external-snapshot-v2:start -->\r?\n"
    r"- checkpoint_id: (?P<checkpoint_id>[^\r\n]+)\r?\n"
    r"- updated: (?P<updated>[^\r\n]+)\r?\n"
    r"- summary: (?P<summary>[^\r\n]*)\r?\n"
    r"- open_intents: (?P<intents>[^\r\n]*)\r?\n"
    r"<!-- engram-external-snapshot-v2:end -->",
)


def _external_path_lock(path: Path) -> threading.Lock:
    with _EXTERNAL_PATH_LOCKS_GUARD:
        return _EXTERNAL_PATH_LOCKS.setdefault(str(path.resolve()).lower(), threading.Lock())


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
    return "---\ntags:\n  - engram\n---\n# To do list\n"


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


def _clean_journal_text(value: str) -> str:
    return str(value or "").strip()


def _snapshot_line(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", _clean_journal_text(value))


def _call_journal_claude(prompt: str) -> str:
    # stm_promoter imports core.memory.store, which imports this module.
    # Keep this one dependency lazy to avoid that real circular import.
    from core.graph.semantic.stm_promoter import _call_claude_once

    return _call_claude_once(prompt, timeout=60.0)


def _automatic_journal_from_transcript(messages: list[dict[str, object]]) -> dict[str, str] | None:
    """Summarize exactly this session's user/assistant transcript into a journal."""
    turns = [{"role": str(row.get("role", "")), "content": _clean_journal_text(str(row.get("content", "")))} for row in messages]
    turns = [row for row in turns if row["role"] in {"user", "assistant"} and row["content"]]
    if not _is_meaningful_transcript(turns):
        return None
    prompt = (
        "다음은 하나의 종료된 세션의 user/assistant 메시지다. 이 메시지 밖의 정보는 절대 사용하지 말고, "
        "사람이 읽는 작업 일지 JSON 하나만 반환하라. 키: title, background, work, result, next. "
        "각 값은 한국어 간결한 문장/마크다운이며 알 수 없는 항목은 빈 문자열. PID, watchdog, 자동 체크포인트, 파일 URI, 내부 도구 이름을 쓰지 마라.\n"
        + json.dumps(turns, ensure_ascii=False)
    )
    response = _call_journal_claude(prompt)
    if not response:
        return None
    try:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, flags=re.IGNORECASE | re.DOTALL)
        parsed = json.loads(match.group(1) if match else response)
    except (TypeError, ValueError):
        return None
    result = {key: _clean_journal_text(parsed.get(key, "")) for key in ("title", "background", "work", "result", "next")}
    visible = "\n".join(result.values()).lower()
    if any(token in visible for token in ("watchdog", "자동 체크포인트", "auto-checkpoint", "auto checkpoint", "file://", "pid", "stm")):
        return None
    return result if result["title"] and (result["work"] or result["result"]) else None


def _is_meaningful_transcript(messages: list[dict[str, object]]) -> bool:
    turns = [row for row in messages if row.get("role") in {"user", "assistant"} and _clean_journal_text(str(row.get("content", "")))]
    return (any(row["role"] == "user" for row in turns) and any(row["role"] == "assistant" for row in turns)
            and len("\n".join(_clean_journal_text(str(row["content"])) for row in turns)) >= 20)


def _explicit_journal(summary: str, progress: str, open_intents: str) -> dict[str, str] | None:
    work = _clean_journal_text(progress) or _clean_journal_text(summary)
    result = _clean_journal_text(summary)
    next_step = _clean_journal_text(open_intents)
    if not any((work, result, next_step)):
        return None
    title = _clean_journal_text(summary).splitlines()[0].lstrip("# ")[:80] or "작업 기록"
    return {"title": title, "background": "", "work": work, "result": result, "next": next_step}


def _external_project_identity(project_key: str, project_node_id: str | None) -> str:
    if project_node_id:
        normalized_node = re.sub(r"[^A-Za-z0-9._:-]+", "-", project_node_id.strip()).strip("-")
        if normalized_node:
            return f"node:{normalized_node}"
    normalized = re.sub(r"[^a-z0-9]+", "-", project_key.strip().lower()).strip("-")
    return f"key:{normalized}" if normalized else "general"


def _external_project_title(project_key: str, project_node_id: str | None, kg: object) -> str:
    if project_node_id:
        try:
            node = kg.get_node(project_node_id)
            title = str((node or {}).get("title", "")).strip()
            if title:
                return _snapshot_line(title)
        except Exception:
            pass
    normalized = re.sub(r"[-_]+", " ", project_key.strip()).strip()
    return _snapshot_line(normalized.title() if normalized else "General")


def _external_v2_block(identity: str, title: str, checkpoint_id: str, now: datetime, summary: str, open_intents: str, *, newline: str) -> str:
    return newline.join((
        f"<!-- engram-external-project-v2:{identity} -->", f"## {_snapshot_line(title)}",
        "<!-- engram-external-snapshot-v2:start -->",
        f"- checkpoint_id: {_snapshot_line(checkpoint_id)}", f"- updated: {now.isoformat()}", f"- summary: {_snapshot_line(summary)}",
        f"- open_intents: {_snapshot_line(open_intents)}",
        "<!-- engram-external-snapshot-v2:end -->",
    ))


def _atomic_replace_external(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_external_text(path: Path) -> str:
    # Path.read_text uses universal-newline translation and would rewrite every
    # legacy CRLF byte during an otherwise-local v2 update.
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _external_newline(text: str) -> str:
    crlf_count = text.count("\r\n")
    lf_count = len(re.findall(r"(?<!\r)\n", text))
    return "\r\n" if crlf_count > lf_count else "\n"


def _upsert_external_project_snapshot(path: Path, *, identity: str, title: str, checkpoint_id: str, now: datetime, summary: str, open_intents: str) -> bool:
    """Atomically replace one v2 project snapshot without touching legacy blocks."""
    with _external_path_lock(path):
        text = _read_external_text(path) if path.exists() else _external_daily_initial()
        newline = _external_newline(text)
        matches = list(_V2_BLOCK_RE.finditer(text))
        # Any unparseable v2 marker means we cannot safely preserve block bounds.
        if text.count("<!-- engram-external-project-v2:") != len(matches):
            return False
        if len({match.group("identity") for match in matches}) != len(matches):
            return False
        block = _external_v2_block(identity, title, checkpoint_id, now, summary, open_intents, newline=newline)
        existing = next((match for match in matches if match.group("identity") == identity), None)
        if existing:
            if existing.group("checkpoint_id") == checkpoint_id:
                return False
            try:
                previous = datetime.fromisoformat(existing.group("updated"))
            except ValueError:
                return False
            try:
                is_older = now < previous
            except TypeError:
                # A valid ISO value with incompatible timezone awareness cannot
                # be safely ordered; preserve the user file unchanged.
                return False
            if is_older:
                return False
            if existing.group(0) == block:
                return False
            updated = text[:existing.start()] + block + text[existing.end():]
        else:
            # Preserve every legacy byte (including trailing whitespace and
            # repeated newlines) before adding the new project snapshot.
            separator = "" if text.endswith(("\n", "\r")) else newline
            updated = text + separator + newline + block + newline
        try:
            _atomic_replace_external(path, updated)
        except OSError:
            return False
        return True


def append_daily_checkpoint(
    *,
    checkpoint_id: str,
    now: datetime,
    summary: str,
    open_intents: str,
    project_key: str,
    project_node_id: str | None,
    external_daily_dir: str = "",
    journal_transcript: list[dict[str, object]] | None = None,
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
        # This is an opt-in integration.  Never turn a typo (or a stale
        # configuration value) into a newly-created external vault hierarchy.
        if external_root.is_dir():
            external_path = external_root / f"{day}.md"
            # Automatic callers (notably the bubble's watchdog close) must
            # journal the human conversation, never the watchdog label.
            journal = _automatic_journal_from_transcript(journal_transcript or [])
            journal = journal or _explicit_journal(summary, "", open_intents)
            external_summary = (journal or {}).get("result") or summary
            external_intents = (journal or {}).get("next") or open_intents
            external_written = _upsert_external_project_snapshot(
                external_path,
                identity=_external_project_identity(project_key, project_node_id),
                title=_external_project_title(project_key, project_node_id, kg),
                checkpoint_id=checkpoint_id, now=now, summary=external_summary, open_intents=external_intents,
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


def append_session_close_daily_note(
    *,
    session_id: int,
    now: datetime,
    summary: str,
    open_intents: str = "",
    progress: str = "",
    transcript: list[dict[str, object]] | None = None,
    automatic: bool = False,
    scope_key: str = "",
    project_key: str = "",
    project_label: str = "",
    project_node_id: str | None = None,
) -> dict[str, object]:
    """Record a completed STM session in the managed daily note.

    All concrete session-close frontends delegate to ``core.memory.close_session``.
    Keeping the note write behind this single coordinator gives retries a stable
    marker and avoids each frontend independently writing the same note.
    """
    resolved_project = project_key or scope_key or "general"
    display_project = project_label or resolved_project
    meaningful_automatic = automatic and _is_meaningful_transcript(transcript or [])
    journal = _automatic_journal_from_transcript(transcript or []) if automatic else _explicit_journal(summary, progress, open_intents)
    if journal is None and not meaningful_automatic:
        return {"engram_path": "", "engram_written": False, "external_path": "", "external_written": False, "project_written": False}
    checkpoint_id = f"session-close-{int(session_id)}"
    result = append_daily_checkpoint(
        checkpoint_id=checkpoint_id, now=now, summary=(journal["result"] or journal["work"]) if journal else "의미 있는 세션 종료",
        open_intents=journal["next"] if journal else "", project_key=resolved_project, project_node_id=project_node_id,
        external_daily_dir="",
    )
    # The final checkpoint is the sole external-journal owner.  Session close
    # keeps its managed ledger marker only, so it cannot duplicate the same
    # human-facing Daily Note entry.
    return result
