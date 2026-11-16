"""kg_update_node 의 frontmatter summary / ## Progress 패치가 YAML·정규식 안전한지 검증."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.graph.knowledge.knowledge_graph import KnowledgeGraph

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

BASE = """---
id: node-1
title: "Sample: Title"
type: concept
tags: [a, b]
summary: old summary
updated: 2020-01-01
---

# Sample

body text

## Progress

> 마지막 업데이트: 2020-01-01 00:00

old progress

## Other

keep me
"""


def _kg() -> KnowledgeGraph:
    return KnowledgeGraph.__new__(KnowledgeGraph)  # DB 초기화 없이 메서드만 사용


def _fm(text: str) -> dict:
    m = _FM.match(text)
    assert m, text
    return yaml.safe_load(m.group(1))


def _patch(tmp_path: Path, summary: str, progress: str = "", open_intents: str = "", base: str = BASE) -> str:
    p = tmp_path / "n.md"
    p.write_text(base, encoding="utf-8")
    _kg()._patch_md_progress(p, summary=summary, progress=progress, open_intents=open_intents)
    return p.read_text(encoding="utf-8")


SUMMARIES = [
    "a: b",
    r"C:\Users\x",
    "# head",
    "line1\nline2",
    "[bracket start",
    "{brace start",
    "*star start",
    "한글 요약: 콜론 # 해시 포함",
    "plain",
]


@pytest.mark.parametrize("summary", SUMMARIES)
def test_summary_round_trips_and_keeps_other_keys(tmp_path, summary):
    out = _patch(tmp_path, summary)
    fm = _fm(out)
    assert fm["summary"] == summary
    assert fm["id"] == "node-1"
    assert fm["title"] == "Sample: Title"
    assert fm["type"] == "concept"
    assert fm["tags"] == ["a", "b"]
    assert "## Other" in out and "keep me" in out


def test_progress_and_open_intents_with_backslashes(tmp_path):
    progress = r"fixed C:\Users\x\new and \1 \g<0> \n"
    intents = r"next: D:\work\tmp \d"
    out = _patch(tmp_path, "ok", progress=progress, open_intents=intents)
    assert progress in out
    assert intents in out
    assert "old progress" not in out
    assert "keep me" in out
    assert _fm(out)["summary"] == "ok"


def test_preexisting_multiline_summary_fully_replaced(tmp_path):
    base = BASE.replace(
        "summary: old summary\n",
        "summary: >\n  folded line one\n  folded line two\n\n  after blank\n",
    )
    assert yaml.safe_load(_FM.match(base).group(1))["summary"].startswith("folded")
    out = _patch(tmp_path, "new one")
    assert "folded" not in out and "after blank" not in out
    fm = _fm(out)
    assert fm["summary"] == "new one"
    assert fm["updated"] is not None and fm["id"] == "node-1"


def test_preexisting_quoted_multiline_summary_replaced(tmp_path):
    base = BASE.replace("summary: old summary\n", 'summary: "first\n  second"\n')
    out = _patch(tmp_path, "x: y", base=base)
    assert "second" not in out
    assert _fm(out)["summary"] == "x: y"


def test_missing_summary_key_is_appended(tmp_path):
    base = BASE.replace("summary: old summary\n", "")
    out = _patch(tmp_path, "a: b", base=base)
    fm = _fm(out)
    assert fm["summary"] == "a: b"
    assert fm["tags"] == ["a", "b"]


def test_failure_does_not_raise_and_leaves_file_unchanged(tmp_path, monkeypatch):
    p = tmp_path / "n.md"
    p.write_text(BASE, encoding="utf-8")
    import core.graph.knowledge.knowledge_graph as kgmod

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(kgmod, "touch_frontmatter_updated", boom)
    _kg()._patch_md_progress(p, summary="s", progress="", open_intents="")
    assert p.read_text(encoding="utf-8") == BASE
