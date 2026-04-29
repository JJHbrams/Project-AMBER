from __future__ import annotations

import pandas as pd
import streamlit as st

from core.dashboard.data_access import query, read_vault_file
from core.dashboard.semantic_api import sg_api, sg_search


def render_semantic() -> None:
    st.title("🌐 Semantic")

    tab_search, tab_neighbors = st.tabs(["🔍 시맨틱 검색", "🔗 노드 유사 이웃"])

    with tab_search:
        q_text = st.text_input("검색어 (자연어)", placeholder="예: 연속체의 기억 구조")
        c1, c2 = st.columns(2)
        top_k = c1.slider("결과 수", 3, 20, 8, key="sk1")
        threshold = c2.slider("유사도 임계값", 0.1, 0.9, 0.30, 0.05, key="sk2")

        if q_text.strip():
            results = sg_search(q_text.strip(), top_k=top_k, threshold=threshold)
            if results and not results[0].get("error"):
                for r in results:
                    with st.expander(f"[{r['score']:.3f}]  **{r['title']}**  `{r['type']}`"):
                        st.write(r.get("summary", ""))
                        node_row = query("SELECT * FROM kg_nodes WHERE id=?", (r["id"],))
                        if node_row:
                            content = read_vault_file(node_row[0].get("path"), node_row[0].get("vault_path"))
                            if content:
                                st.markdown(content)
            else:
                st.info("결과 없음 — 임계값을 낮추거나 다른 키워드를 시도해보세요")

    with tab_neighbors:
        nodes_list = query("SELECT id, title, type FROM kg_nodes ORDER BY title")
        if not nodes_list:
            st.info("KG 노드 없음")
        else:
            opts = {f"{n['title']}  ({n['type']})": n["id"] for n in nodes_list}
            sel_label = st.selectbox("기준 노드", list(opts.keys()))
            top_k_n = st.slider("이웃 수", 3, 15, 8, key="nk")

            if sel_label:
                node_id = opts[sel_label]
                res_n = sg_api("/api/sg/neighbors", method="POST", json_body={"node_id": node_id, "top_k": top_k_n})
                neighbors = (res_n or {}).get("results", [])
                if neighbors:
                    df_n = pd.DataFrame(neighbors)[["title", "type", "score", "summary"]]
                    st.dataframe(df_n, use_container_width=True)
                else:
                    st.info("시맨틱 이웃 없음 (임베딩 없거나 KuzuDB 비활성)")

