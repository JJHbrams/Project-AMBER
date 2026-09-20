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
from core.context.project_scope import resolve_kg_node_id
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
        "---\n"
    )


def _external_daily_initial() -> str:
    return "---\ntags:\n  - engram\n---\n# To do list\n"


def _display_project(project_key: str) -> str:
    """사람이 읽는 프로젝트 이름. 경로 digest 접미사를 뗀다.

    project_key 는 `<디렉터리명>-<경로 sha1 앞 8자>` 라서 같은 이름의 다른 경로를
    구분하지만, 읽는 쪽에는 잡음이다. 내부 키는 그대로 두고 표시만 줄인다.
    """
    return re.sub(r"-[0-9a-f]{8}$", "", str(project_key or "").strip())


# 하나의 체크포인트 항목은 주석 한 줄과 시각 제목 한 줄로 시작한다. 시각 줄만
# 앵커로 쓰면 새 항목이 기존 항목의 주석과 제목 사이로 끼어들어 둘이 엉킨다.
_ENTRY_RE = re.compile(
    r"^<!-- engram-checkpoint:[^\r\n]*-->\r?\n## (?P<time>\d{1,2}:\d{2})[^\r\n]*$",
    re.MULTILINE,
)
# 수평 공백만 먹는다. ``\s*$`` 는 줄바꿈까지 삼켜서 매치 끝이 다음 줄로 밀리고,
# 그 자리에 블록을 끼울 때마다 빈 줄이 하나씩 늘어난다.
_H1_RE = re.compile(r"^# (?P<name>[^\r\n]+?)[^\S\r\n]*$", re.MULTILINE)


def _section_heading(project_key: str, project_keys: list[str] | None) -> str:
    """체크포인트가 들어갈 프로젝트 섹션 이름.

    ``project_keys`` 는 등장 순서가 아니라 **많이 나온 순**이라 앞이 대표다.
    한 구간에 여러 프로젝트가 걸리는 것은 대화별 세션 분리 이전의 잔재이고 지금은
    거의 생기지 않는다. 그 드문 경우에도 섹션은 대표 하나만 쓴다 — 같은 요약을
    프로젝트마다 복제하면 파일이 부풀고 어디가 원본인지 알 수 없게 된다. 나머지
    프로젝트는 본문 ``- 프로젝트:`` 줄에 전부 남으므로 기록이 사라지지는 않는다.
    """
    for key in list(project_keys or []) + [project_key]:
        name = _display_project(key)
        if name:
            return name
    return "general"


def _project_links(project_keys: list[str] | None, project_key: str, project_node_id: str) -> str:
    """본문에 적을 프로젝트 목록. 노드가 확인된 것만 위키링크로 건다.

    추측 링크는 무관한 문서를 가리키므로, 해소되지 않으면 이름만 남긴다.

    프로젝트가 하나면 호출부가 이미 해석해 넘긴 ``project_node_id`` 가 정답이다.
    여러 개인 구간에서만 호출부가 None 을 주므로(요약이 엉킨 채로 어느 한 노드의
    Progress 를 덮지 않기 위해서다), 그때만 키별로 다시 해석한다.
    """
    keys = [key for key in (project_keys or []) if key] or [project_key]
    names = list(dict.fromkeys(name for name in (_display_project(key) for key in keys) if name))
    if not names:
        return ""
    if len(names) == 1:
        return f"[[{project_node_id}]]" if project_node_id else names[0]

    rendered: list[str] = []
    seen: set[str] = set()
    for key in keys:
        name = _display_project(key)
        if not name or name in seen:
            continue
        seen.add(name)
        node = resolve_kg_node_id(key)
        rendered.append(f"[[{node}]]" if node else name)
    return ", ".join(rendered)


def _checkpoint_block(
    checkpoint_id: str,
    now: datetime,
    summary: str,
    open_intents: str,
    project_label: str,
    project_node_id: str = "",
    related_path: Path | None = None,
    project_keys: list[str] | None = None,
) -> str:
    lines = [
        f"<!-- engram-checkpoint:{checkpoint_id} -->",
        f"## {now.strftime('%H:%M')}",
        f"- 요약: {summary}",
    ]
    if open_intents:
        lines.append(f"- 다음 작업: {open_intents}")
    # 프로젝트는 cwd 에서 나온 사실이라 항상 적는다. 섹션 헤딩은 대표 하나만
    # 보여주므로, 구간에 섞인 나머지는 이 줄에서만 확인할 수 있다.
    links = _project_links(project_keys, project_label, project_node_id)
    if links:
        lines.append(f"- 프로젝트: {links}")
    if related_path is not None:
        lines.append(f"- 연관 노트: [{related_path.name}]({related_path.as_uri()})")
    return "\n".join(lines)


def _minutes(value: str) -> int:
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute)


def _insert_by_time(section: str, block: str, minutes: int) -> str:
    """섹션을 항목 단위로 풀었다가 시각 순서로 다시 짠다.

    이어붙이지 않고 매번 다시 렌더링하는 이유는 빈 줄이 새지 않게 하기 위해서다.
    끼워넣기를 반복하면 항목 사이 간격이 호출 순서에 따라 제각각이 된다.
    """
    starts = [match.start() for match in _ENTRY_RE.finditer(section)]
    preamble = (section[:starts[0]] if starts else section).strip()
    bounds = starts + [len(section)]
    entries = [
        (_minutes(_ENTRY_RE.match(section, begin).group("time")), section[begin:end].strip())
        for begin, end in zip(bounds, bounds[1:])
    ]
    position = next((i for i, (at, _) in enumerate(entries) if at > minutes), len(entries))
    entries.insert(position, (minutes, block.strip()))

    parts = ([preamble] if preamble else []) + [body for _, body in entries]
    return "\n\n" + "\n\n".join(parts) + "\n"


def _upsert_project_section(
    path: Path, marker: str, initial: str, heading: str, block: str, minutes: int
) -> bool:
    """``# <프로젝트>`` 섹션 밑에 ``## HH:MM`` 소제목으로 체크포인트를 기록한다.

    하루를 시각 순으로 죽 늘어놓으면 한 프로젝트가 어떻게 흘러갔는지 읽으려고
    무관한 항목을 건너뛰어야 한다. 프로젝트를 바깥 축으로 두면 각 프로젝트의
    하루가 한 덩어리로 읽힌다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else initial
    if marker in text:
        return False

    headings = list(_H1_RE.finditer(text))
    target = next((m for m in headings if m.group("name").strip() == heading), None)
    if target is None:
        updated = f"{text.rstrip()}\n\n# {heading}\n\n{block.strip()}\n"
    else:
        following = next((m for m in headings if m.start() > target.start()), None)
        start = target.end()
        end = following.start() if following else len(text)
        body = _insert_by_time(text[start:end], block, minutes)
        updated = text[:start] + body + ("\n" + text[end:] if following else "")
    path.write_text(updated, encoding="utf-8")
    return True


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
    # digest 를 먼저 뗀다. 안 그러면 .title() 이 해시를 단어로 만들어
    # "Session Agent Orchestration 88Eff352" 같은 제목이 나온다.
    normalized = re.sub(r"[-_]+", " ", _display_project(project_key)).strip()
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
    project_keys: list[str] | None = None,
) -> dict[str, object]:
    """project_keys: 이 구간에 실제로 등장한 프로젝트 전부(많이 나온 순).

    대화별 세션 분리 이후로는 구간마다 프로젝트가 하나로 수렴하지만, 분리 이전에
    열린 세션은 아직 여러 개를 담는다. 대표 하나만 적으면 나머지 작업이 기록에서
    사라지므로, 섹션은 대표로 고르되 본문에는 전부 남긴다.
    """
    day = now.strftime("%Y-%m-%d")
    docs_root = Path(get_db_root_dir()) / "docs"
    engram_path = docs_root / "daily" / f"{day}.md"
    marker = f"engram-checkpoint:{checkpoint_id}"
    engram_block = _checkpoint_block(
        checkpoint_id,
        now,
        summary,
        open_intents,
        project_key,
        project_node_id=project_node_id or "",
        project_keys=project_keys,
    )
    engram_written = _upsert_project_section(
        engram_path,
        marker,
        _engram_daily_initial(day, project_node_id or ""),
        _section_heading(project_key, project_keys),
        engram_block,
        now.hour * 60 + now.minute,
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
