from __future__ import annotations

import pandas as pd
import streamlit as st

from core.dashboard.data_access import parse_tags, query, read_vault_file


def render_wiki_nodes() -> None:
    st.title("📝 Wiki Nodes")

    f_col1, f_col2 = st.columns([1, 3])
    type_filter = f_col1.selectbox(
        "타입",
        ["전체", "concept", "project", "research", "reference", "tool", "person", "moc", "fleeting"],
    )
    search_q = f_col2.text_input("제목 / 요약 검색", placeholder="키워드 입력…")

    sql_q = "SELECT id, title, type, summary, tags, path, vault_path FROM kg_nodes"
    conditions: list[str] = []
    params: list = []
    if type_filter != "전체":
        conditions.append("type=?")
        params.append(type_filter)
    if search_q.strip():
        conditions.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([f"%{search_q}%", f"%{search_q}%"])
    if conditions:
        sql_q += " WHERE " + " AND ".join(conditions)
    sql_q += " ORDER BY title"

    rows = query(sql_q, params)
    if not rows:
        st.info("노드 없음")
    else:
        df = pd.DataFrame(rows)[["title", "type", "summary", "tags"]]
        event = st.dataframe(
            df,
            use_container_width=True,
            selection_mode="single-row",
            on_select="rerun",
            key="kg_table",
            height=300,
        )

        sel = event.selection.rows
        if sel:
            node = rows[sel[0]]
            st.divider()
            st.subheader(f"📄 {node['title']}")
            st.caption(f"`{node['id']}` · {node['type']}")

            tags = parse_tags(node.get("tags"))
            if tags:
                st.markdown(" ".join(f"`{t}`" for t in tags))

            if node.get("summary"):
                st.info(node["summary"])

            content = read_vault_file(node.get("path"), node.get("vault_path"))
            if content:
                with st.expander("📃 vault 원문", expanded=True):
                    st.markdown(content)
            else:
                st.warning("vault 파일 없음 (path 미설정 또는 파일 삭제됨)")

            edges_out = query("SELECT to_id, rel_type, context FROM kg_edges WHERE from_id=?", (node["id"],))
            edges_in = query("SELECT from_id, rel_type, context FROM kg_edges WHERE to_id=?", (node["id"],))
            if edges_out or edges_in:
                with st.expander(f"🔗 연결 관계 ({len(edges_out)+len(edges_in)}개)"):
                    if edges_out:
                        st.markdown("**→ 나가는 엣지**")
                        for e in edges_out:
                            label = f"`{e['rel_type']}` → `{e['to_id']}`"
                            if e.get("context"):
                                label += f"  _{e['context']}_"
                            st.markdown(f"- {label}")
                    if edges_in:
                        st.markdown("**← 들어오는 엣지**")
                        for e in edges_in:
                            label = f"`{e['from_id']}` → `{e['rel_type']}`"
                            if e.get("context"):
                                label += f"  _{e['context']}_"
                            st.markdown(f"- {label}")

