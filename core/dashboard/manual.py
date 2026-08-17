"""Pure helpers for the dashboard's vault-backed manual."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MANUAL_RELATIVE_DIR = Path("docs/guides/engram-manual")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_INVALID_NAMES = {"manifest.md", "manifest.yaml", "manifest.yml"}


@dataclass(frozen=True)
class ManualPage:
    page_id: str
    title: str
    summary: str
    category: str
    tags: tuple[str, ...]
    links: tuple[str, ...]
    relative_path: Path
    content: str


@dataclass(frozen=True)
class ManualCatalog:
    root: Path
    pages: dict[str, ManualPage]
    aliases: dict[str, str]


def _safe_relative(root: Path, candidate: Path) -> Path | None:
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
        return resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None


def is_manual_file(root: Path, candidate: Path) -> bool:
    relative = _safe_relative(root, candidate)
    if relative is None or candidate.suffix.lower() != ".md":
        return False
    name = candidate.name.lower()
    return not (
        name in _INVALID_NAMES
        or name.startswith(".")
        or name.endswith(("~", ".bak", ".backup"))
        or any(part.startswith(".") for part in relative.parts)
    )


def find_manual_documents(manual_dir: Path) -> list[Path]:
    if not manual_dir.is_dir():
        return []
    return sorted(
        (path.relative_to(manual_dir) for path in manual_dir.rglob("*.md") if path.is_file() and is_manual_file(manual_dir, path)),
        key=lambda path: (path.name.lower() != "index.md", str(path).lower()),
    )


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                metadata = yaml.safe_load("".join(lines[1:index])) or {}
            except yaml.YAMLError:
                return {}, content
            return (metadata if isinstance(metadata, dict) else {}), "".join(lines[index + 1:]).lstrip("\r\n")
    return {}, content


def strip_yaml_frontmatter(content: str) -> str:
    return parse_frontmatter(content)[1]


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def normalize_page_target(target: str) -> str:
    """Normalize an Obsidian target without allowing a path escape."""
    normalized = target.strip().replace("\\", "/")
    if normalized.lower().endswith(".md"):
        normalized = normalized[:-3]
    normalized = normalized.strip("/")
    if not normalized or any(part in {".", ".."} for part in normalized.split("/")):
        return ""
    return normalized.lower()


def build_catalog(manual_dir: Path) -> ManualCatalog:
    pages: dict[str, ManualPage] = {}
    aliases: dict[str, str] = {}
    for relative in find_manual_documents(manual_dir):
        text = read_manual_document(manual_dir, relative, include_frontmatter=True)
        metadata, content = parse_frontmatter(text)
        fallback_id = str(relative.with_suffix("")).replace("\\", "/")
        page_id = _text(metadata.get("id"), fallback_id)
        if page_id in pages:
            continue
        title = _text(metadata.get("title"), relative.stem.replace("-", " ").replace("_", " ").title())
        summary = _text(metadata.get("summary"))
        page = ManualPage(
            page_id=page_id,
            title=title,
            summary=summary,
            category=_text(metadata.get("category"), "General"),
            tags=_strings(metadata.get("tags")),
            links=_strings(metadata.get("links")),
            relative_path=relative,
            content=content,
        )
        pages[page_id] = page
        for alias in (
            page_id,
            title,
            relative.stem,
            fallback_id,
            str(relative).replace("\\", "/"),
            *_strings(metadata.get("aliases")),
        ):
            normalized = normalize_page_target(alias)
            if normalized:
                aliases.setdefault(normalized, page_id)
    return ManualCatalog(manual_dir, pages, aliases)


def read_manual_document(manual_dir: Path, relative_path: Path, *, include_frontmatter: bool = False) -> str:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return ""
    candidate = manual_dir / relative_path
    if not is_manual_file(manual_dir, candidate) or not candidate.is_file():
        return ""
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    return text if include_frontmatter else strip_yaml_frontmatter(text)


def resolve_page(catalog: ManualCatalog, target: str) -> ManualPage | None:
    page_id = catalog.aliases.get(normalize_page_target(target))
    return catalog.pages.get(page_id) if page_id else None


def search_pages(catalog: ManualCatalog, query: str) -> list[ManualPage]:
    terms = [term.lower() for term in query.split() if term]
    pages = list(catalog.pages.values())
    if terms:
        pages = [
            page for page in pages
            if all(term in " ".join((page.title, page.summary, page.category, *page.tags, page.content)).lower() for term in terms)
        ]
    return sorted(pages, key=lambda page: (page.category.lower(), page.title.lower()))


def pages_by_category(catalog: ManualCatalog) -> dict[str, list[ManualPage]]:
    result: dict[str, list[ManualPage]] = {}
    for page in search_pages(catalog, ""):
        result.setdefault(page.category, []).append(page)
    return result


def heading_toc(content: str) -> list[tuple[int, str, str]]:
    used: dict[str, int] = {}
    result: list[tuple[int, str, str]] = []
    for match in _HEADING.finditer(content):
        title = re.sub(r"[*_`\[\]]", "", match.group(2)).strip()
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-") or "section"
        used[slug] = used.get(slug, 0) + 1
        anchor = slug if used[slug] == 1 else f"{slug}-{used[slug]}"
        result.append((len(match.group(1)), title, anchor))
    return result


def split_mermaid_blocks(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    cursor = 0
    pattern = re.compile(r"^```mermaid\s*\r?\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL | re.IGNORECASE)
    for match in pattern.finditer(content):
        if match.start() > cursor:
            parts.append(("markdown", content[cursor:match.start()]))
        parts.append(("mermaid", match.group(1).strip()))
        cursor = match.end()
    if cursor < len(content):
        parts.append(("markdown", content[cursor:]))
    return parts or [("markdown", content)]


def wiki_links_to_markdown(content: str, catalog: ManualCatalog) -> tuple[str, list[str]]:
    broken: list[str] = []

    def replace(match: re.Match[str]) -> str:
        target, label = match.group(1).strip(), (match.group(2) or match.group(1)).strip()
        page = resolve_page(catalog, target)
        if page is None:
            broken.append(target)
            return f"{label} (없는 페이지: {target})"
        return f"[{label}](?page=manual&manual={page.page_id})"

    return _WIKI_LINK.sub(replace, content), broken


def push_history(history: list[str], position: int, page_id: str) -> tuple[list[str], int]:
    if position >= 0 and position < len(history) and history[position] == page_id:
        return history, position
    updated = history[: position + 1] + [page_id]
    return updated, len(updated) - 1


def tutorial_status_summary(status: dict) -> str:
    state = status.get("state", {}) if isinstance(status, dict) else {}
    if not isinstance(state, dict):
        return "Tutorial status is unavailable."
    completed = state.get("completed_steps", [])
    skipped = state.get("skipped_steps", [])
    done = len(set(completed if isinstance(completed, list) else []) | set(skipped if isinstance(skipped, list) else []))
    current = str(state.get("current_step", "") or "not started").replace("_", " ")
    return f"Tutorial progress: {done}/4 · Current step: {current}"
