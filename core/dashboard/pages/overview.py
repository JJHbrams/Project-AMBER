from __future__ import annotations

import streamlit as st

from core.dashboard.data_access import query
from core.dashboard.semantic_api import sg_stats


def render_overview() -> None:
    st.title("📊 Overview")

    id_rows = query("SELECT * FROM identity LIMIT 1")
    if id_rows:
        r = id_rows[0]
        st.subheader(f"🧬 {r.get('name') or 'engram'}")
        with st.expander("narrative", expanded=True):
            st.write(r.get("narrative", "—"))
        st.divider()

    cols = st.columns(5)
    for col, (table, label) in zip(
        cols,
        [
            ("kg_nodes", "🗂️ KG Nodes"),
            ("kg_edges", "🔗 Edges"),
            ("memories", "💭 Memories"),
            ("directives", "📋 Directives"),
            ("curiosities", "❓ Curiosities"),
        ],
    ):
        cnt = query(f"SELECT COUNT(*) as c FROM {table}")[0]["c"]
        col.metric(label, cnt)

    stats = sg_stats()
    if stats.get("enabled"):
        st.caption(
            f"🧬 SemanticGraph — KGNode: {stats.get('kg_nodes', 0)}  "
            f"EpisodeNode: {stats.get('episode_nodes', 0)}  "
            f"EP→KG 엣지: {stats.get('ep_to_kg', 0)}  "
            f"KG 엣지: {stats.get('kg_edges', 0)}"
        )
    else:
        st.caption("🧬 SemanticGraph: MCP 서버 연결 대기 중…")

    st.divider()

    col_mem, col_dir = st.columns(2)
    with col_mem:
        st.subheader("💭 최근 기억")
        mems = query("SELECT * FROM memories ORDER BY created_at DESC LIMIT 8")
        if mems:
            for m in mems:
                with st.expander(f"#{m['id']}  {m.get('created_at','')[:16]}"):
                    st.write(m["content"])
        else:
            st.info("저장된 기억 없음")

    with col_dir:
        st.subheader("📋 활성 지시문")
        dirs_ = query("SELECT * FROM directives WHERE active=1 ORDER BY priority DESC")
        if dirs_:
            for d in dirs_:
                with st.expander(f"`{d['key']}` [{d.get('scope','all')}]"):
                    st.caption(d.get("content", "")[:200])
        else:
            st.info("활성 지시문 없음")

    st.divider()
    st.subheader("🗒️ Working Memory")
    wm_rows = query(
        "SELECT scope_key, summary, open_intents, updated_at, expires_at "
        "FROM working_memory "
        "WHERE expires_at IS NULL OR expires_at > datetime('now','localtime') "
        "ORDER BY updated_at DESC"
    )
    if wm_rows:
        for wm in wm_rows:
            scope_label = wm.get("scope_key") or "—"
            updated = (wm.get("updated_at") or "")[:16]
            expires = wm.get("expires_at") or ""
            expires_label = f"  ·  만료 {expires[:16]}" if expires else ""
            with st.expander(f"`{scope_label}`  {updated}{expires_label}", expanded=False):
                summary_text = wm.get("summary") or ""
                intents_text = wm.get("open_intents") or ""
                if summary_text:
                    st.markdown("**요약**")
                    st.write(summary_text)
                if intents_text:
                    st.markdown("**미완료 의도**")
                    st.write(intents_text)
                if not summary_text and not intents_text:
                    st.caption("내용 없음")
    else:
        st.info("활성 working memory 없음")

