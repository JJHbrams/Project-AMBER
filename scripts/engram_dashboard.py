"""
engram_dashboard.py — Streamlit 통합 대시보드

Usage:
    conda run -n intel_engram streamlit run scripts/engram_dashboard.py

Pages:
    📊 Overview      — identity + 테이블 통계 + 최근 기억
    🕸️  KG Graph      — pyvis 인터랙티브 그래프 (시맨틱 엣지 토글, memory 레이어)
    📝 Wiki Nodes    — kg_nodes 브라우징 + 원문 읽기 + 연결 관계
    💭 Memories      — 에피소드 기억 전문
    📋 Directives    — 운영 지시문
    🌐 Semantic      — 시맨틱 검색 + 노드별 유사 이웃
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ── config ────────────────────────────────────────────────────────────────────

DB_PATH = "D:/intel_engram/engram.db"

st.set_page_config(
    page_title="engram",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── dark theme CSS patch ───────────────────────────────────────────────────────
st.markdown(
    """
<style>
[data-testid="stSidebar"] { background: #0d1117; }
.stDataFrame { font-size: 13px; }
</style>
""",
    unsafe_allow_html=True,
)


# ── cached resources ──────────────────────────────────────────────────────────


@st.cache_resource
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# MCP 서버 HTTP API 베이스 URL — MCP server가 KuzuDB 유일 소유자
_MCP_BASE = "http://127.0.0.1:17385"


def _sg_api(path: str, method: str = "GET", json_body: dict | None = None) -> dict | None:
    """MCP server HTTP API 호출. 실패 시 None 반환 (graceful degradation)."""
    import urllib.request
    import json as _json
    url = _MCP_BASE + path
    try:
        if method == "GET":
            req = urllib.request.Request(url)
        else:
            data = _json.dumps(json_body or {}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception:
        return None


@st.cache_data(ttl=30, show_spinner=False)
def _sg_stats() -> dict:
    """MCP /api/sg/stats — 30초 캐시."""
    return _sg_api("/api/sg/stats") or {"enabled": False}


@st.cache_data(ttl=60, show_spinner="시맨틱 그래프 로딩…")
def _sg_graph() -> dict:
    """MCP /api/sg/graph — 60초 캐시."""
    return _sg_api("/api/sg/graph") or {"enabled": False, "kg_nodes": [], "kg_edges": [], "ep_nodes": [], "ep_edges": []}


def _sg_search(q: str, top_k: int = 5, threshold: float = 0.30) -> list:
    """MCP /api/sg/search — 캐시 없음 (실시간 검색)."""
    res = _sg_api("/api/sg/search", method="POST", json_body={"q": q, "top_k": top_k, "threshold": threshold})
    if res is None:
        return [{"error": "MCP 서버 연결 실패 — semantic 기능 불가"}]
    return res.get("results", [])


# ── helpers ───────────────────────────────────────────────────────────────────


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


def _parse_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return [raw]


def _md_to_html(text: str) -> str:
    """기본 마크다운 → HTML 변환 (tooltip 렌더링용)."""
    # 코드 블록 (```...```) 제거 전 처리
    text = re.sub(r'```[\w]*\n?', '', text)
    # 인라인 코드
    text = re.sub(r'`([^`]+)`', r'<code style="background:#161b22;padding:1px 4px;border-radius:3px;font-size:11px">​\1</code>', text)
    # 헤더 (### ## #)
    text = re.sub(r'^### (.+)$', r'<strong style="color:#79c0ff;font-size:11px">▸ \1</strong>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$',  r'<strong style="color:#58a6ff;font-size:12px">▸ \1</strong>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$',   r'<strong style="color:#58a6ff;font-size:13px">▸ \1</strong>', text, flags=re.MULTILINE)
    # 굵게 + 기울임
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', text)
    # 리스트
    text = re.sub(r'^\s*[-*] (.+)$', r'<span style="color:#8b949e">•</span> \1', text, flags=re.MULTILINE)
    # 가로선
    text = re.sub(r'^---+$', r'<hr style="border:none;border-top:1px solid #30363d;margin:4px 0">', text, flags=re.MULTILINE)
    # 줄바껼
    text = text.replace('\n', '<br>')
    return text


def _memory_nodes_edges(show_semantic: bool = True) -> tuple[list[dict], list[dict]]:
    """identity / memories / directives / curiosities → 그래프 노드/엣지 변환.

    show_semantic=True:  semantic + keyword EP_TO_KG 엣지 모두 표시
    show_semantic=False: keyword EP_TO_KG 엣지만 표시 (semantic 제외),
                         keyword도 없으면 identity 허브 fallback.
    엣지 타입별 시각 구분은 build_visjs_html에서 처리.
    """
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

    # ── EP_TO_KG 릴레이션 데이터 조회 (MCP HTTP API) ──────────────────────────
    ep_to_kg_map: dict[str, list[dict]] = {}  # episode_id → [{kg_id, rel_type}]
    graph_data = _sg_graph()
    for ep_edge in graph_data.get("ep_edges", []):
        eid = ep_edge.get("from", "")
        kid = ep_edge.get("to", "")
        rtype = ep_edge.get("rel_type") or "semantic"
        if eid and kid:
            ep_to_kg_map.setdefault(eid, []).append({"kg_id": kid, "rel_type": rtype})

    has_ep_to_kg = bool(ep_to_kg_map)

    # ── memories ─────────────────────────────────────────────────────────────
    for r in query("SELECT * FROM memories ORDER BY created_at DESC"):
        nid = f"memory::mem_{r['id']}"
        content = r.get("content", "")
        extra_nodes.append({"id": nid, "title": f"🧠 {content[:35]}…", "type": "memory", "summary": content[:200], "tags": []})

        mem_id_str = str(r["id"])
        if has_ep_to_kg and mem_id_str in ep_to_kg_map:
            # show_semantic=False이면 keyword 타입만 유지, semantic은 제외
            links = ep_to_kg_map[mem_id_str]
            if not show_semantic:
                links = [lk for lk in links if lk["rel_type"] == "keyword"]
            if links:
                for link in links:
                    extra_edges.append({"from": nid, "to": link["kg_id"], "rel_type": link["rel_type"], "context": ""})
            else:
                # keyword도 없으면 identity 허브 fallback
                if id_row:
                    extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_memory", "context": ""})
        else:
            # EP_TO_KG 없는 memory → identity 허브에 fallback 연결 (항상)
            if id_row:
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


# ── graph builder ─────────────────────────────────────────────────────────────

NODE_COLORS_FALLBACK = {
    "concept": "#6c8ebf",
    "project": "#82b366",
    "research": "#d6b656",
    "reference": "#ae4132",
    "tool": "#9673a6",
    "person": "#d79b00",
    "moc": "#006eaf",
    "fleeting": "#888888",
}
MEMORY_NODE_COLORS = {
    "identity": "#ffd700",
    "memory": "#c678dd",
    "directive": "#e5c07b",
    "curiosity": "#61afef",
}
EDGE_COLORS = {
    "links": "#58a6ff",
    "supports": "#3fb950",
    "contradicts": "#f85149",
    "part_of": "#d2a8ff",
    "follows": "#ffa657",
    "inspired_by": "#79c0ff",
    "implements": "#56d364",
    "references": "#8b949e",
    "semantic": "#a29bfe",
    "keyword": "#e5c07b",
    "has_memory": "#c678dd",
    "has_directive": "#e5c07b",
    "has_curiosity": "#61afef",
}


def build_visjs_html(
    nodes: list[dict],
    edges: list[dict],
    height: int = 600,
    physics_enabled: bool = True,
    grav_const: int = -60,
    central_gravity: float = 0.005,
    spring_length: int = 140,
    spring_const: float = 0.06,
    damping: float = 0.4,
    size_scale: float = 1.0,
) -> str:
    """순수 vis.js HTML 그래프 생성기.
    - JS 내장 실시간 컨트롤 패널 (physics, 노드 크기 배율)
    - 엣지 타입별 시각 구분: KG=solid colored, semantic=dashed purple, keyword=dotted orange
    - hover tooltip (vis.js 기본 title 속성)
    """
    vis_js_path = _ROOT / "lib" / "vis-9.1.2" / "vis-network.min.js"
    vis_css_path = _ROOT / "lib" / "vis-9.1.2" / "vis-network.css"
    vis_js = vis_js_path.read_text(encoding="utf-8") if vis_js_path.exists() else ""
    vis_css = vis_css_path.read_text(encoding="utf-8") if vis_css_path.exists() else ""

    try:
        from core.knowledge_graph import NODE_COLORS
    except Exception:
        NODE_COLORS = NODE_COLORS_FALLBACK

    in_degree: dict[str, int] = defaultdict(int)
    for e in edges:
        in_degree[e.get("to") or e.get("to_id", "")] += 1

    # ── 노드 데이터 ──────────────────────────────────────────────────────────
    vis_nodes: list[dict] = []
    added: set[str] = set()
    for n in nodes:
        nid = n["id"]
        if nid in added:
            continue
        added.add(nid)
        ntype = n.get("type", "concept")
        color = MEMORY_NODE_COLORS.get(ntype) or NODE_COLORS.get(ntype, "#95a5a6")
        if ntype == "identity":
            base_size, shape = 40, "star"
        elif ntype in MEMORY_NODE_COLORS:
            base_size, shape = 18, "diamond"
        else:
            base_size = 12 + min(in_degree[nid] * 5, 35)
            shape = "dot"
        size = int(base_size * size_scale)
        tags = _parse_tags(n.get("tags"))
        summary_html = _md_to_html(n.get("summary", "")[:400])
        tooltip = (
            f'<div class="eg-tip">'
            f'<div class="eg-tip-title">{n["title"]}</div>'
            f'<div class="eg-tip-type">{ntype}</div>'
            f'<div class="eg-tip-body">{summary_html}</div>'
            + (f'<div class="eg-tip-tags">🏷 {", ".join(tags)}</div>' if tags else "")
            + '</div>'
        )
        vis_nodes.append({
            "id": nid,
            "label": n["title"],
            "_tooltip": tooltip,
            "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            "size": size,
            "_baseSize": base_size,
            "shape": shape,
            "font": {"color": "#c9d1d9"},
        })

    # ── 엣지 데이터 (타입별 시각 구분) ──────────────────────────────────────
    vis_edges: list[dict] = []
    for i, e in enumerate(edges):
        fk = e.get("from") or e.get("from_id", "")
        tk = e.get("to") or e.get("to_id", "")
        if fk not in added or tk not in added:
            continue
        rtype = e.get("rel_type", "links")
        ecolor = EDGE_COLORS.get(rtype, "#30363d")
        # semantic: dashed purple / keyword: dotted orange / 나머지: solid colored
        if e.get("dashes") or rtype == "semantic":
            edge_style = {"dashes": [5, 5], "width": 1.0, "color": {"color": "#a29bfe", "highlight": "#c9b8ff"}}
        elif rtype == "keyword":
            edge_style = {"dashes": [2, 5], "width": 1.0, "color": {"color": "#e5c07b", "highlight": "#ffd580"}}
        else:
            edge_style = {"dashes": False, "width": 1.5, "color": {"color": ecolor, "highlight": "#a29bfe"}}
        label_text = rtype + (f": {e['context']}" if e.get("context") else "")
        vis_edges.append({"id": i, "from": fk, "to": tk, "title": label_text, **edge_style})

    nodes_json = json.dumps(vis_nodes, ensure_ascii=False)
    edges_json = json.dumps(vis_edges, ensure_ascii=False)

    # 슬라이더 초기값 (정수 범위로 변환)
    phys_checked = "checked" if physics_enabled else ""
    abs_grav = abs(grav_const)
    cg_init = int(central_gravity * 1000)
    sc_init = int(spring_const * 100)
    damp_init = int(damping * 100)
    ss_init = int(size_scale * 10)

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>
{vis_css}
body{{margin:0;padding:0;background:#0d1117;color:#c9d1d9;font-family:monospace;overflow:hidden;}}
#mynetwork{{width:100%;height:{height}px;background:#0d1117;}}
.vis-tooltip{{
  background:#161b22 !important;
  border:1px solid #30363d !important;
  border-radius:8px !important;
  padding:0 !important;
  width:300px !important;
  max-width:300px !important;
  white-space:normal !important;
  word-break:break-word !important;
  overflow-wrap:break-word !important;
  box-shadow:0 4px 16px rgba(0,0,0,0.6) !important;
  font-family:monospace !important;
  font-size:12px !important;
  color:#c9d1d9 !important;
  pointer-events:none;
}}
.eg-tip{{ padding:10px 12px; box-sizing:border-box; width:100%; }}
.eg-tip-title{{ font-weight:bold; font-size:13px; color:#e6edf3; margin-bottom:2px; word-break:break-word; white-space:normal; }}
.eg-tip-type{{ font-size:10px; color:#8b949e; margin-bottom:6px; }}
.eg-tip-body{{ color:#c9d1d9; line-height:1.6; max-height:220px; overflow-y:auto; overflow-x:hidden; white-space:normal; word-break:break-word; }}
.eg-tip-body code{{ background:#0d1117; padding:1px 4px; border-radius:3px; }}
.eg-tip-tags{{ margin-top:6px; font-size:10px; color:#8b949e; border-top:1px solid #30363d; padding-top:4px; white-space:normal; word-break:break-word; }}
#eg-pin{{
  display:none; position:absolute; z-index:20;
  width:300px; max-height:380px;
  overflow-y:auto; overflow-x:hidden;
  background:#161b22;
  border:1.5px solid #58a6ff;
  border-radius:8px;
  box-shadow:0 4px 20px rgba(0,0,0,0.8);
  font-family:monospace; font-size:12px; color:#c9d1d9;
  box-sizing:border-box;
  white-space:normal; word-break:break-word;
  cursor:default;
}}
#eg-pin .eg-tip-body{{ max-height:none; overflow-y:visible; }}
#ctrl{{position:absolute;top:8px;right:8px;z-index:10;background:rgba(22,27,34,0.93);
  border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:11px;min-width:158px;
  box-shadow:0 2px 10px rgba(0,0,0,0.6);}}
#ctrl summary{{cursor:pointer;color:#58a6ff;font-weight:bold;user-select:none;outline:none;}}
.cl{{color:#8b949e;margin-top:5px;margin-bottom:1px;display:flex;justify-content:space-between;}}
.cv{{color:#79c0ff;}}
input[type=range]{{width:100%;accent-color:#58a6ff;margin:1px 0;}}
label{{cursor:pointer;}}
hr.sep{{border:none;border-top:1px solid #30363d;margin:6px 0;}}
</style>
<script>{vis_js}</script>
</head>
<body>
<div style="position:relative" id="wrap">
  <div id="mynetwork"></div>
  <div id="eg-pin"></div>
  <details id="ctrl" open>
    <summary>⚙ 실시간 제어</summary>
    <div style="margin-top:6px">
      <label><input type="checkbox" id="physOn" {phys_checked}> Physics</label>
      <div class="cl">반발력 <span class="cv" id="gv">{abs_grav}</span></div>
      <input type="range" id="gs" min="10" max="300" value="{abs_grav}">
      <div class="cl">엣지 길이 <span class="cv" id="slv">{spring_length}</span></div>
      <input type="range" id="sl" min="50" max="400" value="{spring_length}">
      <hr class="sep">
      <div class="cl">노드 크기 <span class="cv" id="ssv">{size_scale:.1f}x</span></div>
      <input type="range" id="ss" min="5" max="30" value="{ss_init}">
    </div>
  </details>
</div>
<script>
var _nodesArr={nodes_json};
var _edgesArr={edges_json};
var _baseSizes={{}};
_nodesArr.forEach(function(n){{
  _baseSizes[n.id]=n._baseSize||n.size||12;
  if(n._tooltip){{var d=document.createElement('div');d.innerHTML=n._tooltip;n.title=d;}}
}});
var _container=document.getElementById('mynetwork');
var _dsN=new vis.DataSet(_nodesArr);
var _dsE=new vis.DataSet(_edgesArr);
var network=new vis.Network(_container,{{nodes:_dsN,edges:_dsE}},{{
  nodes:{{borderWidth:1,borderWidthSelected:3,font:{{size:12,face:"monospace",color:"#c9d1d9"}}}},
  edges:{{arrows:{{to:{{enabled:true,scaleFactor:0.4}}}},smooth:{{type:"continuous",roundness:0.2}}}},
  physics:{{
    enabled:{str(physics_enabled).lower()},
    forceAtlas2Based:{{gravitationalConstant:{grav_const},centralGravity:{central_gravity},
      springLength:{spring_length},springConstant:{spring_const},damping:{damping}}},
    solver:"forceAtlas2Based",
    stabilization:{{enabled:true,iterations:200,fit:true}}
  }},
  interaction:{{hover:true,tooltipDelay:80,navigationButtons:true}}
}});
var _done=false;
function _stopPhys(){{if(!_done){{_done=true;network.setOptions({{physics:{{enabled:false}}}});document.getElementById('physOn').checked=false;}}}}
network.on('stabilizationIterationsDone',_stopPhys);
setTimeout(_stopPhys,4000);
// ── 클릭 핀 패널 ──────────────────────────────────────────────────────
var _pin=document.getElementById('eg-pin');
var _pinnedId=null;
var _visTooltip=null;
function _hideVisTooltip(){{
  if(!_visTooltip) _visTooltip=document.querySelector('.vis-tooltip');
  if(_visTooltip) _visTooltip.style.visibility='hidden';
}}
function _showVisTooltip(){{
  if(_visTooltip) _visTooltip.style.visibility='';
}}
network.on('click',function(params){{
  if(params.nodes.length>0){{
    var nid=params.nodes[0];
    var node=_dsN.get(nid);
    if(node&&node._tooltip){{
      _pin.innerHTML=node._tooltip;
      var cpos=network.canvasToDOM(network.getPosition(nid));
      var netH={height};
      var px=cpos.x+24; var py=Math.max(4,cpos.y-80);
      if(px+304>_container.offsetWidth) px=Math.max(4,cpos.x-324);
      if(py+400>netH) py=Math.max(4,netH-404);
      _pin.style.left=px+'px'; _pin.style.top=py+'px';
      _pin.style.display='block';
      _pinnedId=nid;
      _hideVisTooltip();
    }}
  }} else {{
    _pin.style.display='none';
    _pinnedId=null;
    _showVisTooltip();
    network.unselectAll();
  }}
}});
_pin.addEventListener('click',function(e){{e.stopPropagation();}});
// ── 핀 패널 드래그 ──────────────────────────────────────────────────────
(function(){{
  var _dx=0,_dy=0,_dragging=false;
  _pin.style.cursor='grab';
  _pin.addEventListener('mousedown',function(e){{
    if(e.button!==0) return;
    _dragging=true; _dx=e.clientX-_pin.offsetLeft; _dy=e.clientY-_pin.offsetTop;
    _pin.style.cursor='grabbing'; _pin.style.userSelect='none';
    e.stopPropagation(); e.preventDefault();
  }});
  document.addEventListener('mousemove',function(e){{
    if(!_dragging) return;
    _pin.style.left=(e.clientX-_dx)+'px'; _pin.style.top=(e.clientY-_dy)+'px';
  }});
  document.addEventListener('mouseup',function(e){{
    if(!_dragging) return;
    _dragging=false; _pin.style.cursor='grab'; _pin.style.userSelect='';
  }});
}})();
document.getElementById('physOn').addEventListener('change',function(){{
  network.setOptions({{physics:{{enabled:this.checked}}}});_done=!this.checked;
}});
function _rc(sid,vid,vfn,dfn,afn){{
  document.getElementById(sid).addEventListener('input',function(){{
    var v=vfn(parseFloat(this.value));
    document.getElementById(vid).textContent=dfn?dfn(v):v;
    afn(v);
  }});
}}
_rc('gs','gv',function(v){{return -v;}},function(v){{return Math.round(-v);}},
  function(v){{network.setOptions({{physics:{{forceAtlas2Based:{{gravitationalConstant:v}}}}}});}});
_rc('sl','slv',function(v){{return v;}},function(v){{return Math.round(v);}},
  function(v){{network.setOptions({{physics:{{forceAtlas2Based:{{springLength:v}}}}}});}});
_rc('ss','ssv',function(v){{return v/10;}},function(v){{return v.toFixed(1)+'x';}},
  function(scale){{
    var upd=_nodesArr.map(function(n){{return {{id:n.id,size:Math.round(_baseSizes[n.id]*scale)}};}} );
    _dsN.update(upd);
  }});
</script>
</body></html>"""


# ── sidebar ───────────────────────────────────────────────────────────────────

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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Overview
# ══════════════════════════════════════════════════════════════════════════════
if page == "📊 Overview":
    st.title("📊 Overview")

    id_rows = query("SELECT * FROM identity LIMIT 1")
    if id_rows:
        r = id_rows[0]
        st.subheader(f"🧬 {r.get('name') or 'engram'}")
        with st.expander("narrative", expanded=True):
            st.write(r.get("narrative", "—"))
        st.divider()

    # stats row
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

    # SemanticGraph 통계 (MCP API)
    sg_stats = _sg_stats()
    if sg_stats.get("enabled"):
        st.caption(
            f"🧬 SemanticGraph — KGNode: {sg_stats.get('kg_nodes', 0)}  "
            f"EpisodeNode: {sg_stats.get('episode_nodes', 0)}  "
            f"EP→KG 엣지: {sg_stats.get('ep_to_kg', 0)}  "
            f"KG 엣지: {sg_stats.get('kg_edges', 0)}"
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

    # Working Memory
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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: KG Graph
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🕸️ KG Graph":
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

    # ── Physics 설정 패널 ──────────────────────────────────────────────────
    with st.expander("⚙️ Physics 설정", expanded=False):
        ph_col1, ph_col2 = st.columns(2)
        with ph_col1:
            physics_on = st.checkbox("Physics 활성화", value=True,
                help="끄면 노드가 고정된 위치에 렌더링됩니다 (이미 stabilize된 레이아웃 유지용)")
            grav_const = st.slider("반발력 (Gravitational Constant)", -300, -10, -60, 10,
                help="음수 값이 클수록 노드끼리 강하게 밀어냄")
            spring_length = st.slider("기본 엣지 길이 (Spring Length)", 50, 400, 140, 10,
                help="노드 간 기본 거리. 클수록 그래프가 넓게 펼쳐짐")
            size_scale = st.slider("노드 크기 배율", 0.5, 3.0, 1.0, 0.1,
                help="참조 횟수 기반 노드 크기에 곱해지는 배율")
        with ph_col2:
            central_gravity = st.slider("중심 인력 (Central Gravity)", 0.000, 0.050, 0.005, 0.001,
                format="%.3f",
                help="전체 그래프를 중앙으로 모으는 힘. 0에 가까울수록 자유롭게 흩어짐")
            spring_const = st.slider("엣지 탄성 (Spring Constant)", 0.01, 0.30, 0.06, 0.01,
                help="엣지가 당기는 힘. 클수록 연결된 노드가 더 강하게 붙음")
            damping = st.slider("감쇠 (Damping)", 0.1, 1.0, 0.4, 0.05,
                help="진동 억제. 1.0에 가까울수록 빠르게 정지")

    # ── 그래프 데이터 fetch (버튼) ─────────────────────────────────────────
    if st.button("▶ 그래프 생성", type="primary"):
        with st.spinner("그래프 구성 중…"):
            from core.db import initialize_db

            initialize_db()
            from core.knowledge_graph import get_kg

            kg = get_kg()

            if focus_input.strip():
                focus_node = kg.get_node(focus_input.strip())
                if not focus_node:
                    st.error(f"노드 없음: {focus_input}")
                    st.stop()
                # BFS subgraph
                from collections import deque

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

            # memory layer
            if show_memory:
                mem_nodes, mem_edges = _memory_nodes_edges(show_semantic=show_semantic)
                _nodes = _nodes + mem_nodes
                _edges = _edges + mem_edges

            # semantic edges (MCP HTTP API)
            if show_semantic:
                sg_data = _sg_graph()
                for se in sg_data.get("kg_edges", []):
                    _edges.append({
                        "from": se["from"],
                        "to": se["to"],
                        "rel_type": se.get("rel_type", "semantic"),
                        "context": f"w={se.get('weight', 0):.3f}",
                        "dashes": True,
                    })

            st.session_state["kg_cached_nodes"] = _nodes
            st.session_state["kg_cached_edges"] = _edges

    # ── 렌더링 (캐시된 데이터 + 현재 설정값으로 항상 즉시 반영) ───────────
    if "kg_cached_nodes" in st.session_state:
        nodes = st.session_state["kg_cached_nodes"]
        edges = st.session_state["kg_cached_edges"]
        html = build_visjs_html(
            nodes, edges, height=620,
            physics_enabled=physics_on,
            grav_const=grav_const,
            central_gravity=central_gravity,
            spring_length=spring_length,
            spring_const=spring_const,
            damping=damping,
            size_scale=size_scale,
        )
        st.components.v1.html(html, height=640, scrolling=False)
        st.caption(f"노드: {len(nodes)}  엣지: {len(edges)}")

        # legend
        with st.expander("범례"):
            leg_col1, leg_col2 = st.columns(2)
            with leg_col1:
                st.markdown("**KG Wiki 노드**")
                for ntype, color in NODE_COLORS_FALLBACK.items():
                    st.markdown(
                        f'<span style="color:{color}">●</span> {ntype}',
                        unsafe_allow_html=True,
                    )
            with leg_col2:
                st.markdown("**Memory DB 노드**")
                syms = {"identity": "★", "memory": "◆", "directive": "◆", "curiosity": "●"}
                for mtype, color in MEMORY_NODE_COLORS.items():
                    st.markdown(
                        f'<span style="color:{color}">{syms.get(mtype,"●")}</span> {mtype}',
                        unsafe_allow_html=True,
                    )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Wiki Nodes
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📝 Wiki Nodes":
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

            tags = _parse_tags(node.get("tags"))
            if tags:
                st.markdown(" ".join(f"`{t}`" for t in tags))

            if node.get("summary"):
                st.info(node["summary"])

            # vault 원문
            content = read_vault_file(node.get("path"), node.get("vault_path"))
            if content:
                with st.expander("📃 vault 원문", expanded=True):
                    st.markdown(content)
            else:
                st.warning("vault 파일 없음 (path 미설정 또는 파일 삭제됨)")

            # 연결 관계
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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Memories
# ══════════════════════════════════════════════════════════════════════════════
elif page == "💭 Memories":
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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Directives
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📋 Directives":
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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Semantic
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🌐 Semantic":
    st.title("🌐 Semantic")

    tab_search, tab_neighbors = st.tabs(["🔍 시맨틱 검색", "🔗 노드 유사 이웃"])

    with tab_search:
        q_text = st.text_input("검색어 (자연어)", placeholder="예: 연속체의 기억 구조")
        c1, c2 = st.columns(2)
        top_k = c1.slider("결과 수", 3, 20, 8, key="sk1")
        threshold = c2.slider("유사도 임계값", 0.1, 0.9, 0.30, 0.05, key="sk2")

        if q_text.strip():
            results = _sg_search(q_text.strip(), top_k=top_k, threshold=threshold)
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
                res_n = _sg_api("/api/sg/neighbors", method="POST", json_body={"node_id": node_id, "top_k": top_k_n})
                neighbors = (res_n or {}).get("results", [])
                if neighbors:
                    df_n = pd.DataFrame(neighbors)[["title", "type", "score", "summary"]]
                    st.dataframe(df_n, use_container_width=True)
                else:
                    st.info("시맨틱 이웃 없음 (임베딩 없거나 KuzuDB 비활성)")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Identity
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🧬 Identity":
    st.title("🧬 Identity")

    tab_id, tab_session, tab_schema, tab_full_schema = st.tabs(["🪪 정체성 상태", "📅 세션 / 활동", "🗃️ DB 스키마", "📜 전체 DDL"])

    # ── 정체성 상태 ────────────────────────────────────────────────────────────
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

            # persona JSON
            persona_raw = r.get("persona", "{}")
            try:
                persona_obj = json.loads(persona_raw) if isinstance(persona_raw, str) else persona_raw
            except Exception:
                persona_obj = {}
            if persona_obj:
                st.divider()
                st.markdown("**persona**")
                # 숫자형 슬라이더 필드 (0.0~1.0)
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

        # themes
        themes = query("SELECT * FROM themes ORDER BY weight DESC")
        if themes:
            st.divider()
            st.markdown("**themes**")
            t_df = pd.DataFrame(themes)
            st.dataframe(t_df, use_container_width=True, hide_index=True)

        # working_memory
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

        # curiosities
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

    # ── 세션 / 활동 ───────────────────────────────────────────────────────────
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

    # ── DB 스키마 ─────────────────────────────────────────────────────────────
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

                    # foreign keys
                    fks = get_db().execute(f"PRAGMA foreign_key_list({tname})").fetchall()
                    if fks:
                        st.caption("Foreign keys: " + ", ".join(f"`{fk[3]}` → `{fk[2]}.{fk[4]}`" for fk in fks))

                    # indexes
                    idxs = get_db().execute(f"PRAGMA index_list({tname})").fetchall()
                    if idxs:
                        st.caption("Indexes: " + ", ".join(f"`{idx[1]}`" for idx in idxs))

    # ── 전체 DDL ──────────────────────────────────────────────────────────────
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
