from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from core.dashboard.data_access import get_db, query


def render_identity() -> None:
    st.title("🧬 Identity")

    tab_id, tab_session, tab_schema, tab_full_schema = st.tabs(["🪪 정체성 상태", "📅 세션 / 활동", "🗃️ DB 스키마", "📜 전체 DDL"])

    with tab_id:
        id_row = query("SELECT * FROM identity LIMIT 1")
        if not id_row:
            st.warning("identity 레코드 없음")
        else:
            r = id_row[0]
            col_a, col_b = st.columns([1, 2])
            with col_a:
                st.metric("이름", r.get("name") or "(미설정)")
                st.caption(f"생성: {r.get('created_at','')[:16]}")
                st.caption(f"수정: {r.get('updated_at','')[:16]}")
            with col_b:
                st.markdown("**narrative**")
                st.info(r.get("narrative", "—"))

            persona_raw = r.get("persona", "{}")
            try:
                persona_obj = json.loads(persona_raw) if isinstance(persona_raw, str) else persona_raw
            except Exception:
                persona_obj = {}
            if persona_obj:
                st.divider()
                st.markdown("**persona**")
                numeric_keys = {"warmth", "formality", "humor", "directness"}
                num_items = [(k, v) for k, v in persona_obj.items() if k in numeric_keys and isinstance(v, (int, float))]
                str_items = [(k, v) for k, v in persona_obj.items() if k not in numeric_keys and not isinstance(v, (dict, list)) and k != "name"]
                nested_items = [(k, v) for k, v in persona_obj.items() if isinstance(v, (dict, list))]

                if num_items:
                    n_cols = st.columns(len(num_items))
                    for col, (k, v) in zip(n_cols, num_items):
                        col.metric(k, f"{v:.2f}")
                        col.progress(float(v))

                for k, v in str_items:
                    st.markdown(f"**{k}**")
                    st.caption(str(v))

                for k, v in nested_items:
                    with st.expander(f"`{k}`"):
                        st.json(v)

        themes = query("SELECT * FROM themes ORDER BY weight DESC")
        if themes:
            st.divider()
            st.markdown("**themes**")
            t_df = pd.DataFrame(themes)
            st.dataframe(t_df, use_container_width=True, hide_index=True)

        wm_rows = query("SELECT * FROM working_memory ORDER BY updated_at DESC")
        if wm_rows:
            st.divider()
            st.markdown("**working memory**")
            for wm in wm_rows:
                with st.expander(f"`{wm['scope_key']}`  updated: {wm.get('updated_at','')[:16]}"):
                    if wm.get("summary"):
                        st.markdown("*summary*")
                        st.write(wm["summary"])
                    if wm.get("open_intents"):
                        st.markdown("*open intents*")
                        st.write(wm["open_intents"])

        cur_rows = query("SELECT * FROM curiosities ORDER BY created_at DESC")
        if cur_rows:
            st.divider()
            st.markdown("**curiosities**")
            status_icon = {"pending": "❓", "addressed": "✅", "dismissed": "🚫"}
            for c in cur_rows:
                icon = status_icon.get(c.get("status", "pending"), "❓")
                with st.expander(f"{icon} {c.get('topic','')}"):
                    st.caption(f"status: {c.get('status')}  ·  created: {c.get('created_at','')[:16]}")
                    if c.get("reason"):
                        st.write(c["reason"])

    with tab_session:
        st.markdown("**최근 세션**")
        sessions = query("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 20")
        if sessions:
            s_df = pd.DataFrame(sessions)
            event_s = st.dataframe(
                s_df[["id", "scope_key", "started_at", "ended_at", "summary"]],
                use_container_width=True,
                selection_mode="single-row",
                on_select="rerun",
                key="session_table",
                height=220,
            )
            sel_s = event_s.selection.rows
            if sel_s:
                sess = sessions[sel_s[0]]
                st.divider()
                st.subheader(f"Session #{sess['id']}  —  {sess.get('scope_key','')}")
                if sess.get("summary"):
                    st.info(sess["summary"])
                msgs = query(
                    "SELECT role, content, timestamp FROM messages WHERE session_id=? ORDER BY timestamp",
                    (sess["id"],),
                )
                if msgs:
                    with st.expander(f"메시지 {len(msgs)}개", expanded=True):
                        for msg in msgs:
                            role_icon = {"user": "🧑", "assistant": "🤖", "system": "⚙️"}.get(msg["role"], "💬")
                            st.markdown(f"**{role_icon} {msg['role']}** `{msg.get('timestamp','')[:16]}`")
                            st.write(msg["content"])
                            st.divider()
        else:
            st.info("세션 없음")

        st.divider()
        st.markdown("**활동 로그**")
        act_limit = st.slider("표시 수", 10, 100, 30, key="act_limit")
        acts = query(f"SELECT * FROM activity_log ORDER BY created_at DESC LIMIT {act_limit}")
        if acts:
            a_df = pd.DataFrame(acts)[["id", "actor", "project", "action", "detail", "created_at"]]
            st.dataframe(a_df, use_container_width=True, hide_index=True)
        else:
            st.info("활동 로그 없음")

    with tab_schema:
        st.markdown("SQLite `engram.db` — 테이블별 컬럼 구조")
        tables = [r["name"] for r in query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if not tables:
            st.warning("테이블 없음")
        else:
            for tname in tables:
                cols_info = get_db().execute(f"PRAGMA table_info({tname})").fetchall()
                row_count = get_db().execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
                with st.expander(f"**{tname}**  ({row_count:,} rows)"):
                    col_df = pd.DataFrame(
                        [{"#": c[0], "name": c[1], "type": c[2], "notnull": bool(c[3]), "default": c[4], "pk": bool(c[5])} for c in cols_info]
                    )
                    st.dataframe(col_df, use_container_width=True, hide_index=True)

                    fks = get_db().execute(f"PRAGMA foreign_key_list({tname})").fetchall()
                    if fks:
                        st.caption("Foreign keys: " + ", ".join(f"`{fk[3]}` → `{fk[2]}.{fk[4]}`" for fk in fks))

                    idxs = get_db().execute(f"PRAGMA index_list({tname})").fetchall()
                    if idxs:
                        st.caption("Indexes: " + ", ".join(f"`{idx[1]}`" for idx in idxs))

    with tab_full_schema:
        st.markdown("`sqlite_master` 에서 추출한 전체 CREATE 문")
        ddl_rows = query("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name")
        if not ddl_rows:
            st.warning("DDL 없음")
        else:
            type_groups: dict[str, list] = {}
            for row in ddl_rows:
                type_groups.setdefault(row["type"], []).append(row)

            for obj_type, rows in type_groups.items():
                st.subheader(obj_type.upper())
                for row in rows:
                    with st.expander(f"`{row['name']}`"):
                        st.code(row["sql"], language="sql")

