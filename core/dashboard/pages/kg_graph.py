from __future__ import annotations

import sqlite3
from collections import deque

import streamlit as st

from core.graph.knowledge import get_kg
from core.storage.db import initialize_db
from core.dashboard.data_access import DB_PATH, memory_nodes_edges
from core.dashboard.graph_render import MEMORY_NODE_COLORS, NODE_COLORS_FALLBACK, build_visjs_html
from core.dashboard.semantic_api import sg_graph


def render_kg_graph() -> None:
    st.title("🕸️ KG Graph")

    ctrl1, ctrl2, ctrl3 = st.columns([3, 1, 2])
    focus_input = ctrl1.text_input("Focus 노드 (id 또는 제목, 비우면 전체)", "")
    hops = ctrl2.slider("Hops", 1, 4, 2)
    with ctrl3:
        show_memory = st.checkbox("🧬 Memory 레이어", value=False)
        show_semantic = st.checkbox("🌐 시맨틱 엣지", value=False)

    sem_threshold = 0.55
    if show_semantic:
        sem_threshold = st.slider("시맨틱 유사도 임계값", 0.30, 0.90, 0.55, 0.05)

    with st.expander("⚙️ Physics 설정", expanded=False):
        ph_col1, ph_col2 = st.columns(2)
        with ph_col1:
            physics_on = st.checkbox("Physics 활성화", value=True, help="끄면 노드가 고정된 위치에 렌더링됩니다 (이미 stabilize된 레이아웃 유지용)")
            grav_const = st.slider("반발력 (Gravitational Constant)", -300, -10, -60, 10, help="음수 값이 클수록 노드끼리 강하게 밀어냄")
            spring_length = st.slider("기본 엣지 길이 (Spring Length)", 50, 400, 140, 10, help="노드 간 기본 거리. 클수록 그래프가 넓게 펼쳐짐")
            size_scale = st.slider("노드 크기 배율", 0.5, 3.0, 1.0, 0.1, help="참조 횟수 기반 노드 크기에 곱해지는 배율")
        with ph_col2:
            central_gravity = st.slider(
                "중심 인력 (Central Gravity)",
                0.000,
                0.050,
                0.005,
                0.001,
                format="%.3f",
                help="전체 그래프를 중앙으로 모으는 힘. 0에 가까울수록 자유롭게 흩어짐",
            )
            spring_const = st.slider(
                "엣지 탄성 (Spring Constant)", 0.01, 0.30, 0.06, 0.01, help="엣지가 당기는 힘. 클수록 연결된 노드가 더 강하게 붙음"
            )
            damping = st.slider("감쇠 (Damping)", 0.1, 1.0, 0.4, 0.05, help="진동 억제. 1.0에 가까울수록 빠르게 정지")

    if st.button("▶ 그래프 생성", type="primary"):
        with st.spinner("그래프 구성 중…"):
            initialize_db()
            kg = get_kg()

            if focus_input.strip():
                focus_node = kg.get_node(focus_input.strip())
                if not focus_node:
                    st.error(f"노드 없음: {focus_input}")
                    st.stop()

                raw_conn = sqlite3.connect(DB_PATH)
                raw_conn.row_factory = sqlite3.Row
                visited = {focus_node["id"]}
                bfs = deque([(focus_node["id"], 0)])
                _nodes: list[dict] = [focus_node]
                _edges: list[dict] = []
                while bfs:
                    cur_id, depth = bfs.popleft()
                    if depth >= hops:
                        continue
                    for row in raw_conn.execute(
                        "SELECT e.from_id, e.to_id, e.rel_type, e.context, n.* "
                        "FROM kg_edges e JOIN kg_nodes n ON e.to_id=n.id WHERE e.from_id=?",
                        (cur_id,),
                    ).fetchall():
                        nid = row["to_id"]
                        _edges.append({"from": cur_id, "to": nid, "rel_type": row["rel_type"], "context": row["context"]})
                        if nid not in visited:
                            visited.add(nid)
                            nr = raw_conn.execute("SELECT * FROM kg_nodes WHERE id=?", (nid,)).fetchone()
                            if nr:
                                _nodes.append(dict(nr))
                            bfs.append((nid, depth + 1))
                    for row in raw_conn.execute(
                        "SELECT e.from_id, e.to_id, e.rel_type, e.context, n.* "
                        "FROM kg_edges e JOIN kg_nodes n ON e.from_id=n.id WHERE e.to_id=?",
                        (cur_id,),
                    ).fetchall():
                        nid = row["from_id"]
                        _edges.append({"from": nid, "to": cur_id, "rel_type": row["rel_type"], "context": row["context"]})
                        if nid not in visited:
                            visited.add(nid)
                            nr = raw_conn.execute("SELECT * FROM kg_nodes WHERE id=?", (nid,)).fetchone()
                            if nr:
                                _nodes.append(dict(nr))
                            bfs.append((nid, depth + 1))
                raw_conn.close()
            else:
                _nodes, _edges = kg.dump_graph()
                _nodes = [dict(n) for n in _nodes]
                _edges = [dict(e) for e in _edges]

            if show_memory:
                mem_nodes, mem_edges = memory_nodes_edges(show_semantic=show_semantic)
                _nodes = _nodes + mem_nodes
                _edges = _edges + mem_edges

            if show_semantic:
                sg_data = sg_graph()
                for se in sg_data.get("kg_edges", []):
                    _edges.append(
                        {
                            "from": se["from"],
                            "to": se["to"],
                            "rel_type": se.get("rel_type", "semantic"),
                            "context": f"w={se.get('weight', 0):.3f}",
                            "dashes": True,
                        }
                    )

            st.session_state["kg_cached_nodes"] = _nodes
            st.session_state["kg_cached_edges"] = _edges

    if "kg_cached_nodes" in st.session_state:
        nodes = st.session_state["kg_cached_nodes"]
        edges = st.session_state["kg_cached_edges"]
        html = build_visjs_html(
            nodes,
            edges,
            height=680,
            physics_enabled=physics_on,
            grav_const=grav_const,
            central_gravity=central_gravity,
            spring_length=spring_length,
            spring_const=spring_const,
            damping=damping,
            size_scale=size_scale,
        )
        st.components.v1.html(html, height=700, scrolling=False)
        st.caption(f"노드: {len(nodes)}  엣지: {len(edges)}")

        with st.expander("범례"):
            leg_col1, leg_col2 = st.columns(2)
            with leg_col1:
                st.markdown("**KG Wiki 노드**")
                for ntype, color in NODE_COLORS_FALLBACK.items():
                    st.markdown(f'<span style="color:{color}">●</span> {ntype}', unsafe_allow_html=True)
            with leg_col2:
                st.markdown("**Memory DB 노드**")
                syms = {"identity": "★", "memory": "◆", "directive": "◆", "curiosity": "●"}
                for mtype, color in MEMORY_NODE_COLORS.items():
                    st.markdown(f'<span style="color:{color}">{syms.get(mtype,"●")}</span> {mtype}', unsafe_allow_html=True)

