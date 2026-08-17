from pathlib import Path

from core.dashboard.manual import (
    build_catalog,
    heading_toc,
    push_history,
    resolve_page,
    search_pages,
    split_mermaid_blocks,
    wiki_links_to_markdown,
)


def _write(root: Path, name: str, content: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_catalog_frontmatter_aliases_and_hidden_files(tmp_path: Path):
    _write(tmp_path, "index.md", "---\nid: home\ntitle: Home\nsummary: Landing\ncategory: Start\ntags: [intro]\nlinks: [guide]\naliases: [start-here]\n---\n# Hello")
    _write(tmp_path, "guide.md", "---\nid: setup\ntitle: Setup\ncategory: Guides\n---\nUseful text")
    _write(tmp_path, ".hidden.md", "# hidden")
    _write(tmp_path, "manifest.md", "# manifest")
    catalog = build_catalog(tmp_path)
    assert set(catalog.pages) == {"home", "setup"}
    assert resolve_page(catalog, "index").page_id == "home"
    assert resolve_page(catalog, "guide").page_id == "setup"
    assert resolve_page(catalog, "start-here").page_id == "home"
    assert resolve_page(catalog, "Home").page_id == "home"
    assert catalog.pages["home"].links == ("guide",)


def test_catalog_rejects_paths_outside_root(tmp_path: Path):
    _write(tmp_path, "safe.md", "# Safe")
    catalog = build_catalog(tmp_path)
    assert resolve_page(catalog, "../safe") is None
    assert "safe" in catalog.aliases


def test_obsidian_targets_accept_paths_and_md_suffix(tmp_path: Path):
    _write(tmp_path, "guides/first-page.md", "---\nid: first\ntitle: First page\n---\n# First")
    catalog = build_catalog(tmp_path)
    assert resolve_page(catalog, "guides/first-page.md").page_id == "first"
    assert resolve_page(catalog, "guides\\first-page").page_id == "first"
    assert resolve_page(catalog, "./guides/first-page") is None


def test_search_categories_and_headings(tmp_path: Path):
    _write(tmp_path, "one.md", "---\nid: one\ntitle: First Page\ncategory: Reference\ntags: [network]\n---\n# Intro\n## Detail\n## Detail")
    _write(tmp_path, "two.md", "---\nid: two\ntitle: Second\ncategory: How-to\n---\nnetwork flow")
    catalog = build_catalog(tmp_path)
    assert [page.page_id for page in search_pages(catalog, "network")] == ["two", "one"]
    assert heading_toc(catalog.pages["one"].content) == [(1, "Intro", "intro"), (2, "Detail", "detail"), (2, "Detail", "detail-2")]


def test_wiki_links_resolve_and_broken_links_are_visible(tmp_path: Path):
    _write(tmp_path, "index.md", "---\nid: home\n---\n[[guide|Read guide]] and [[missing]]")
    _write(tmp_path, "guide.md", "---\nid: guide\n---\nGuide")
    catalog = build_catalog(tmp_path)
    rendered, broken = wiki_links_to_markdown(catalog.pages["home"].content, catalog)
    assert "[Read guide](?page=manual&manual=guide)" in rendered
    assert "missing (없는 페이지: missing)" in rendered
    assert broken == ["missing"]


def test_mermaid_split_and_history():
    blocks = split_mermaid_blocks("Before\n```mermaid\ngraph TD\n A-->B\n```\nAfter")
    assert blocks == [("markdown", "Before\n"), ("mermaid", "graph TD\n A-->B"), ("markdown", "\nAfter")]
    history, position = push_history(["home", "guide"], 1, "reference")
    assert (history, position) == (["home", "guide", "reference"], 2)
    history, position = push_history(history, position, "reference")
    assert (history, position) == (["home", "guide", "reference"], 2)


def test_packaged_manual_has_no_broken_wiki_links():
    manual_root = Path(__file__).resolve().parents[1] / "installer" / "templates" / "manual"
    catalog = build_catalog(manual_root)

    broken: list[tuple[str, str]] = []
    for page in catalog.pages.values():
        _, missing = wiki_links_to_markdown(page.content, catalog)
        broken.extend((page.page_id, target) for target in missing)

    assert broken == []
