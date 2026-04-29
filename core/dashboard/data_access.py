from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import streamlit as st

from core.dashboard.semantic_api import sg_graph

DB_PATH = "D:/intel_engram/engram.db"


@st.cache_resource
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params=()) -> list[dict]:
    rows = get_db().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def read_vault_file(path_col: str | None, vault_path_col: str | None) -> str:
    """path 또는 vault_path 컬럼으로 마크다운 원문 읽기."""
    for candidate in (vault_path_col, path_col):
        if not candidate:
            continue
        p = Path(candidate)
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                pass
    return ""


def parse_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return [raw]


def memory_nodes_edges(show_semantic: bool = True) -> tuple[list[dict], list[dict]]:
    """identity / memories / directives / curiosities → 그래프 노드/엣지 변환."""
    extra_nodes: list[dict] = []
    extra_edges: list[dict] = []

    identity_id = "memory::identity"
    id_row = query("SELECT * FROM identity LIMIT 1")
    if id_row:
        r = id_row[0]
        extra_nodes.append(
            {
                "id": identity_id,
                "title": r.get("name") or "engram",
                "type": "identity",
                "summary": r.get("narrative", "")[:200],
                "tags": [],
            }
        )

    ep_to_kg_map: dict[str, list[dict]] = {}
    graph_data = sg_graph()
    for ep_edge in graph_data.get("ep_edges", []):
        eid = ep_edge.get("from", "")
        kid = ep_edge.get("to", "")
        rtype = ep_edge.get("rel_type") or "semantic"
        if eid and kid:
            ep_to_kg_map.setdefault(eid, []).append({"kg_id": kid, "rel_type": rtype})

    has_ep_to_kg = bool(ep_to_kg_map)

    for r in query("SELECT * FROM memories ORDER BY created_at DESC"):
        nid = f"memory::mem_{r['id']}"
        content = r.get("content", "")
        extra_nodes.append({"id": nid, "title": f"🧠 {content[:35]}…", "type": "memory", "summary": content[:200], "tags": []})

        mem_id_str = str(r["id"])
        if has_ep_to_kg and mem_id_str in ep_to_kg_map:
            links = ep_to_kg_map[mem_id_str]
            if not show_semantic:
                links = [lk for lk in links if lk["rel_type"] == "keyword"]
            if links:
                for link in links:
                    extra_edges.append({"from": nid, "to": link["kg_id"], "rel_type": link["rel_type"], "context": ""})
            elif id_row:
                extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_memory", "context": ""})
        elif id_row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_memory", "context": ""})

    for r in query("SELECT * FROM directives WHERE active=1 ORDER BY priority DESC"):
        nid = f"memory::dir_{r['key']}"
        extra_nodes.append(
            {"id": nid, "title": f"📋 {r['key']}", "type": "directive", "summary": r.get("content", "")[:200], "tags": [r.get("scope", "all")]}
        )
        if id_row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_directive", "context": r.get("scope", "")})

    for r in query("SELECT * FROM curiosities WHERE status != 'addressed' ORDER BY created_at DESC"):
        nid = f"memory::cur_{r['id']}"
        extra_nodes.append({"id": nid, "title": f"❓ {r.get('topic','')[:35]}", "type": "curiosity", "summary": r.get("reason", ""), "tags": []})
        if id_row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_curiosity", "context": ""})

    return extra_nodes, extra_edges

