from __future__ import annotations

from pathlib import Path

import streamlit as st

from core.config.runtime_config import get_db_root_dir
from core.dashboard.manual import (
    MANUAL_RELATIVE_DIR,
    ManualCatalog,
    build_catalog,
    heading_toc,
    pages_by_category,
    push_history,
    resolve_page,
    search_pages,
    split_mermaid_blocks,
    wiki_links_to_markdown,
)


def _manual_root() -> Path:
    return Path(get_db_root_dir()) / MANUAL_RELATIVE_DIR


def _set_query_page(page_id: str) -> None:
    st.query_params["page"] = "manual"
    st.query_params["manual"] = page_id


def _set_page(page_id: str) -> None:
    history, position = push_history(st.session_state.get("manual_history", []), st.session_state.get("manual_history_position", -1), page_id)
    st.session_state.manual_history = history
    st.session_state.manual_history_position = position
    _set_query_page(page_id)


def _set_search_query(query: str) -> None:
    st.session_state.manual_main_search = query


def _current_page(catalog: ManualCatalog):
    requested = str(st.query_params.get("manual", "") or "")
    page = resolve_page(catalog, requested) if requested else None
    if page is None:
        page = resolve_page(catalog, "index") or next(iter(catalog.pages.values()), None)
    if page is not None:
        _set_page(page.page_id)
    return page


def _history_buttons(catalog: ManualCatalog) -> None:
    history = st.session_state.get("manual_history", [])
    position = st.session_state.get("manual_history_position", -1)
    back, forward = st.columns(2)
    if back.button("← 이전", disabled=position <= 0, use_container_width=True):
        st.session_state.manual_history_position -= 1
        _set_query_page(history[position - 1])
        st.rerun()
    if forward.button("다음 →", disabled=position < 0 or position >= len(history) - 1, use_container_width=True):
        st.session_state.manual_history_position += 1
        _set_query_page(history[position + 1])
        st.rerun()


def _catalog_navigation(catalog: ManualCatalog) -> None:
    st.sidebar.markdown("### 매뉴얼 페이지")
    query = st.sidebar.text_input("매뉴얼 검색", key="manual_search")
    pages = search_pages(catalog, query)
    if query:
        st.sidebar.caption(f"{len(pages)}개 결과")
    for category, category_pages in pages_by_category(catalog).items():
        selected = [page for page in category_pages if page in pages]
        if not selected:
            continue
        with st.sidebar.expander(category, expanded=bool(query)):
            for page in selected:
                if st.button(page.title, key=f"manual-nav-{page.page_id}", use_container_width=True):
                    _set_page(page.page_id)
                    st.rerun()


def _main_search(catalog: ManualCatalog) -> None:
    query = st.text_input("매뉴얼 검색", placeholder="페이지, 주제, 태그로 검색", key="manual_main_search")
    if not query.strip():
        return
    results = search_pages(catalog, query)
    st.subheader("검색 결과")
    if not results:
        st.info("검색어와 일치하는 매뉴얼 페이지가 없습니다.")
        return
    for result in results:
        label = result.title if not result.summary else f"{result.title} — {result.summary}"
        if st.button(label, key=f"manual-result-{result.page_id}", use_container_width=True):
            _set_page(result.page_id)
            st.rerun()
        st.caption(result.category + (f" · {', '.join(result.tags)}" if result.tags else ""))


def _render_page(catalog: ManualCatalog, page) -> None:
    st.title(page.title)
    if page.summary:
        st.caption(page.summary)
    metadata = [page.category, *page.tags]
    if metadata:
        st.caption(" · ".join(metadata))
    if page.links:
        related = []
        for target in page.links:
            target_page = resolve_page(catalog, target)
            related.append(
                f"[{target_page.title}](?page=manual&manual={target_page.page_id})"
                if target_page is not None
                else f"{target} (없는 페이지)"
            )
        st.markdown("**관련 페이지:** " + " · ".join(related))
    toc = heading_toc(page.content)
    if toc:
        with st.expander("이 페이지의 내용", expanded=False):
            st.markdown("\n".join(f"{'  ' * (level - 1)}- [{title}](#{anchor})" for level, title, anchor in toc))
    broken: list[str] = []
    for kind, block in split_mermaid_blocks(page.content):
        if kind == "mermaid":
            st.mermaid_chart(block)
        else:
            rendered, missing = wiki_links_to_markdown(block, catalog)
            broken.extend(missing)
            st.markdown(rendered)
    if broken:
        st.warning("찾을 수 없는 매뉴얼 링크: " + ", ".join(sorted(set(broken))))


def render_manual() -> None:
    st.header("📘 매뉴얼")
    root = _manual_root()
    catalog = build_catalog(root)
    if not catalog.pages:
        st.error("Wiki vault에 매뉴얼 페이지가 설치되어 있지 않습니다.")
        st.info("Installer를 다시 실행해 관리형 매뉴얼 페이지를 복구한 뒤 대시보드를 새로고침하세요.")
        return
    _catalog_navigation(catalog)
    _main_search(catalog)
    _history_buttons(catalog)
    page = _current_page(catalog)
    if page is None:
        st.error("읽을 수 있는 매뉴얼 페이지를 찾지 못했습니다.")
        return
    crumb_manual, crumb_category, crumb_page = st.columns([1, 2, 4])
    if crumb_manual.button("매뉴얼", key="manual-crumb-home", use_container_width=True):
        home = resolve_page(catalog, "index") or next(iter(catalog.pages.values()))
        _set_page(home.page_id)
        st.rerun()
    crumb_category.button(
        page.category,
        key="manual-crumb-category",
        use_container_width=True,
        on_click=_set_search_query,
        args=(page.category,),
    )
    crumb_page.caption(page.title)
    _render_page(catalog, page)
