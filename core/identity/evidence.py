"""Read-only, privacy-bounded identity evidence for self-reflection surfaces."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from core.common.sanitizer import sanitize

_ALLOWED_WIKI_NODE: Final = "engram-연속체-정체성-시스템"
_NARRATIVE_MAX: Final = 280
_THEME_MAX: Final = 48
_SUMMARY_MAX: Final = 220


def _clean(value: object, limit: int) -> str:
    return sanitize(" ".join(str(value or "").split()), max_length=limit).strip()


@dataclass(frozen=True)
class IdentityEvidence:
    """Only fields explicitly approved for a self-reflection prompt."""

    name: str
    narrative: str
    themes: tuple[str, ...]
    wiki_title: str = ""
    wiki_type: str = ""
    wiki_tags: tuple[str, ...] = ()
    wiki_summary: str = ""

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def has_content(self) -> bool:
        return bool(self.narrative or self.themes or self.wiki_summary)


def get_self_reflection_evidence() -> IdentityEvidence | None:
    """Return bounded identity-only evidence, or ``None`` on any read failure.

    This deliberately does not use reflection preparation, memory retrieval, wiki
    bodies, or arbitrary KG search.  The single node lookup is allowlisted and
    returns metadata only.
    """
    try:
        # These storage-backed imports are intentionally lazy: this API's
        # contract is to fail closed with None even when identity/KG storage is
        # unavailable during an optional overlay initiative poll.
        from .service import get_identity, get_themes
        from core.graph.knowledge.knowledge_graph import get_kg

        identity = get_identity() or {}
        name = _clean(identity.get("name"), 50)
        narrative = _clean(identity.get("narrative"), _NARRATIVE_MAX)
        themes = tuple(
            item for item in (_clean(label, _THEME_MAX) for label, _weight in get_themes(4)) if item
        )
        node = get_kg().get_node(_ALLOWED_WIKI_NODE) or {}
        if node and str(node.get("id") or "") != _ALLOWED_WIKI_NODE:
            return None
        raw_tags = node.get("tags") if node else []
        if not isinstance(raw_tags, list):
            return None
        evidence = IdentityEvidence(
            name=name,
            narrative=narrative,
            themes=themes,
            wiki_title=_clean(node.get("title"), 80),
            wiki_type=_clean(node.get("type"), 32),
            wiki_tags=tuple(item for item in (_clean(tag, 32) for tag in raw_tags[:6]) if item),
            wiki_summary=_clean(node.get("summary"), _SUMMARY_MAX),
        )
        return evidence if evidence.has_content else None
    except Exception:
        return None
