from __future__ import annotations

import streamlit as st

from core.dashboard.data_access import query


def render_directives() -> None:
    st.title("📋 Directives")

    show_inactive = st.checkbox("비활성 지시문도 표시", value=False)
    sql_d = "SELECT * FROM directives" + ("" if show_inactive else " WHERE active=1")
    sql_d += " ORDER BY active DESC, priority DESC"
    dirs_ = query(sql_d)

    if not dirs_:
        st.info("지시문 없음")
    else:
        for d in dirs_:
            active_badge = "🟢" if d.get("active") else "🔴"
            scope = d.get("scope", "all")
            priority = d.get("priority", 0)
            header = f"{active_badge} `{d['key']}` &nbsp; scope=`{scope}` &nbsp; priority={priority}"
            with st.expander(header):
                st.markdown(d.get("content", ""), unsafe_allow_html=False)

