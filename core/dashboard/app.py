"""
core.dashboard — Streamlit 통합 대시보드 (엔트리)

직접 실행 시:
    conda run -n intel_engram streamlit run core/dashboard/app.py

overlay 통합 런처 경유:
    streamlit run scripts/engram_dashboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = Path(__file__).resolve().parent / "assets"
sys.path.insert(0, str(_ROOT))

from core.dashboard.pages import (  # noqa: E402
    render_directives,
    render_identity,
    render_kg_graph,
    render_memories,
    render_overview,
    render_semantic,
    render_wiki_nodes,
)

st.set_page_config(
    page_title="engram",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

_sidebar_css = (_ASSETS / "dashboard.css").read_text(encoding="utf-8")
st.markdown(f"<style>\n{_sidebar_css}</style>", unsafe_allow_html=True)

st.sidebar.markdown("## 🧠 engram")
st.sidebar.markdown("---")
page = st.sidebar.radio(
    "nav",
    ["📊 Overview", "🕸️ KG Graph", "📝 Wiki Nodes", "💭 Memories", "📋 Directives", "🌐 Semantic", "🧬 Identity"],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
if st.sidebar.button("🔄 DB 캐시 초기화"):
    st.cache_resource.clear()
    st.cache_data.clear()
    st.rerun()

if page == "📊 Overview":
    render_overview()
elif page == "🕸️ KG Graph":
    render_kg_graph()
elif page == "📝 Wiki Nodes":
    render_wiki_nodes()
elif page == "💭 Memories":
    render_memories()
elif page == "📋 Directives":
    render_directives()
elif page == "🌐 Semantic":
    render_semantic()
elif page == "🧬 Identity":
    render_identity()

