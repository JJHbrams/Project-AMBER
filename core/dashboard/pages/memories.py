from __future__ import annotations

import streamlit as st

from core.dashboard.data_access import query


def render_memories() -> None:
    st.title("💭 Memories")

    mems = query("SELECT * FROM memories ORDER BY created_at DESC")
    if not mems:
        st.info("저장된 기억 없음")
    else:
        for m in mems:
            ts = m.get("created_at", "")[:16]
            sid = m.get("session_id")
            header = f"#{m['id']}  {ts}" + (f"  ·  session {sid}" if sid else "")
            with st.expander(header):
                st.write(m["content"])

