"""Content contract for the install-managed manual corpus.

These checks intentionally use Korean-friendly structural thresholds instead of
English word counts. They protect the manual from silently regressing to a
bare index while allowing wording to evolve with the implementation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / "installer" / "templates" / "manual"
EXPECTED_FILES = {
    "index.md",
    "getting-started.md",
    "architecture.md",
    "session-memory.md",
    "wiki-kg.md",
    "overlay-settings.md",
    "skills-agents.md",
    "mcp-tools.md",
    "dashboard.md",
    "installation-update.md",
    "self-diagnosis.md",
}
DIAGNOSIS_PAGES = EXPECTED_FILES
REQUIRED_FRONTMATTER = {
    "id", "title", "note_type", "tags", "summary", "aliases", "links",
    "manual_version", "category",
}
WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n.*?\n```", re.DOTALL)
FORBIDDEN_LOCAL_PATTERNS = (
    re.compile(r"(?i)[a-z]:\\\\users\\"),
    re.compile(r"(?i)c:\\users\\"),
    re.compile(r"(?i)d:\\intel_engram"),
    re.compile(r"(?i)127\.0\.0\.1:\\d+"),
    re.compile(r"(?i)character:\s*\n\s*(?:name|set):\s*[\"']?(?!<)[a-z0-9_-]+"),
)


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    assert match, f"missing YAML frontmatter: {path.name}"
    metadata = yaml.safe_load(match.group(1))
    assert isinstance(metadata, dict), f"frontmatter must be a mapping: {path.name}"
    return metadata, match.group(2)


def test_manifest_exactly_describes_the_manual_corpus():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["manual_version"] == "1.1.0"
    assert set(manifest["files"]) == EXPECTED_FILES
    assert len(manifest["files"]) == len(EXPECTED_FILES)
    assert {path.name for path in ROOT.glob("*.md")} == EXPECTED_FILES


def test_every_page_has_required_metadata_and_resolvable_wiki_links():
    by_id: dict[str, Path] = {}
    page_data: list[tuple[Path, dict, str]] = []
    for filename in sorted(EXPECTED_FILES):
        metadata, body = _frontmatter(ROOT / filename)
        assert REQUIRED_FRONTMATTER <= metadata.keys(), filename
        assert isinstance(metadata["tags"], list) and 1 <= len(metadata["tags"]) <= 3, filename
        assert isinstance(metadata["aliases"], list) and metadata["aliases"], filename
        assert isinstance(metadata["links"], list), filename
        assert metadata["id"] not in by_id, f"duplicate id: {metadata['id']}"
        by_id[str(metadata["id"])] = ROOT / filename
        page_data.append((ROOT / filename, metadata, body))

    aliases = set(by_id)
    for path, metadata, _ in page_data:
        aliases.add(path.stem)
        aliases.update(str(item) for item in metadata.get("aliases", []))
    for path, metadata, body in page_data:
        for target in metadata["links"]:
            assert str(target) in aliases, f"unresolved frontmatter link {target!r} in {path.name}"
        for target in WIKI_LINK.findall(body):
            assert target.strip() in aliases, f"unresolved wiki link {target!r} in {path.name}"


def test_page_versions_match_the_manifest():
    manifest_version = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["manual_version"]
    for filename in EXPECTED_FILES:
        metadata, _ = _frontmatter(ROOT / filename)
        version = str(metadata["manual_version"])
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), filename
        assert version == manifest_version, filename


def test_manual_has_substantive_korean_friendly_content_and_diagnosis_sections():
    for filename in EXPECTED_FILES:
        _, body = _frontmatter(ROOT / filename)
        headings = re.findall(r"^##+\s+.+$", body, re.MULTILINE)
        assert len(body.strip()) >= 1800, f"manual body is too short: {filename}"
        assert len(headings) >= 3, f"manual needs useful sections: {filename}"
    for filename in DIAGNOSIS_PAGES:
        _, body = _frontmatter(ROOT / filename)
        headings = re.findall(r"^##\s+(.+)$", body, re.MULTILINE)
        assert any("기대 동작" in heading for heading in headings), filename
        assert any(any(word in heading for word in ("증상", "이상", "문제", "실패")) for heading in headings), filename
        assert any(any(word in heading for word in ("점검", "확인", "절차")) for heading in headings), filename
        assert any("복구" in heading for heading in headings), filename
        assert any("관련" in heading for heading in headings), filename


def test_all_diagrams_have_text_alternatives_and_no_local_specific_values():
    for filename in EXPECTED_FILES:
        _, body = _frontmatter(ROOT / filename)
        blocks = list(MERMAID_BLOCK.finditer(body))
        assert blocks, f"expected a Mermaid diagram: {filename}"
        for block in blocks:
            after = body[block.end():]
            assert re.match(r"\s*텍스트 대체 설명:", after), f"missing immediate text alternative: {filename}"
        for pattern in FORBIDDEN_LOCAL_PATTERNS:
            assert not pattern.search(body), f"local-specific value found in {filename}: {pattern.pattern}"
