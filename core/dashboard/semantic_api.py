from __future__ import annotations

import json
import os
import urllib.request

import streamlit as st

def _sg_api(path: str, method: str = "GET", json_body: dict | None = None) -> dict | None:
    """MCP server HTTP API 호출. 실패 시 None 반환 (graceful degradation)."""
    # AppTest renders the real dashboard during packaging, but must not query
    # the interactive user's running MCP (nor open an unrelated local service).
    if os.environ.get('ENGRAM_BUILD_SMOKE') == '1':
        return None
    try:
        from core.install.service_config import effective_service_config
        port = effective_service_config()['mcp']['http_port']
        url = f'http://127.0.0.1:{port}' + path
        if method == "GET":
            req = urllib.request.Request(url)
        else:
            data = json.dumps(json_body or {}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


@st.cache_data(ttl=30, show_spinner=False)
def sg_stats() -> dict:
    """MCP /api/sg/stats — 30초 캐시."""
    return _sg_api("/api/sg/stats") or {"enabled": False}


@st.cache_data(ttl=60, show_spinner="시맨틱 그래프 로딩…")
def sg_graph() -> dict:
    """MCP /api/sg/graph — 60초 캐시."""
    return _sg_api("/api/sg/graph") or {"enabled": False, "kg_nodes": [], "kg_edges": [], "ep_nodes": [], "ep_edges": []}


def sg_search(q: str, top_k: int = 5, threshold: float = 0.30) -> list:
    """MCP /api/sg/search — 캐시 없음 (실시간 검색)."""
    res = _sg_api("/api/sg/search", method="POST", json_body={"q": q, "top_k": top_k, "threshold": threshold})
    if res is None:
        return [{"error": "MCP 서버 연결 실패 — semantic 기능 불가"}]
    return res.get("results", [])


def sg_api(path: str, method: str = "GET", json_body: dict | None = None) -> dict | None:
    return _sg_api(path, method=method, json_body=json_body)

