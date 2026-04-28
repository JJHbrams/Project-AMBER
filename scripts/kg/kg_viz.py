"""
kg_viz.py — Knowledge Graph 인터랙티브 시각화

Usage:
    python scripts/kg_viz.py [--vault D:\\intel_engram] [--output D:\\intel_engram\\docs\\kg_graph.html]
    python scripts/kg_viz.py --focus "connectome" --hops 2   # 특정 노드 중심 서브그래프
    python scripts/kg_viz.py --memory                        # memory DB 레이어 포함 (identity/memories/directives)
"""

import sys
import argparse
from pathlib import Path
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from core.db import initialize_db
from core.knowledge_graph import get_kg, NODE_COLORS

try:
    from pyvis.network import Network
except ImportError:
    print("❌ pyvis 없음. pip install pyvis 실행하세요.")
    sys.exit(1)


BG_COLOR = "#0d1117"
FONT_COLOR = "#c9d1d9"
EDGE_COLOR = "#30363d"
EDGE_HIGHLIGHT = "#a29bfe"

EDGE_COLORS = {
    "links": "#58a6ff",
    "supports": "#3fb950",
    "contradicts": "#f85149",
    "part_of": "#d2a8ff",
    "follows": "#ffa657",
    "inspired_by": "#79c0ff",
    "implements": "#56d364",
    "references": "#8b949e",
    # memory layer
    "has_memory": "#c678dd",
    "has_directive": "#e5c07b",
    "has_curiosity": "#61afef",
}

MEMORY_NODE_COLORS = {
    "identity": "#ffd700",
    "memory": "#c678dd",
    "directive": "#e5c07b",
    "curiosity": "#61afef",
}


def load_memory_layer() -> tuple[list, list]:
    """identity / memories / directives / curiosities를 그래프 노드로 변환"""
    from core.db import get_connection
    import json as _json

    conn = get_connection()
    extra_nodes: list[dict] = []
    extra_edges: list[dict] = []

    # ── identity ──────────────────────────────────────────
    row = conn.execute("SELECT * FROM identity LIMIT 1").fetchone()
    identity_id = "memory::identity"
    if row:
        row = dict(row)
        extra_nodes.append(
            {
                "id": identity_id,
                "title": row.get("name") or "engram",
                "type": "identity",
                "summary": row.get("narrative", "")[:200],
                "tags": [],
            }
        )

    # ── memories ──────────────────────────────────────────
    for r in conn.execute("SELECT * FROM memories ORDER BY created_at DESC").fetchall():
        r = dict(r)
        nid = f"memory::mem_{r['id']}"
        content = r.get("content", "")
        extra_nodes.append(
            {
                "id": nid,
                "title": f"🧠 {content[:35]}…",
                "type": "memory",
                "summary": content[:200],
                "tags": [],
            }
        )
        if row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_memory", "context": ""})

    # ── directives ────────────────────────────────────────
    for r in conn.execute("SELECT * FROM directives WHERE active=1 ORDER BY priority DESC").fetchall():
        r = dict(r)
        nid = f"memory::dir_{r['key']}"
        extra_nodes.append(
            {
                "id": nid,
                "title": f"📋 {r['key']}",
                "type": "directive",
                "summary": r.get("content", "")[:200],
                "tags": [r.get("scope", "all")],
            }
        )
        if row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_directive", "context": r.get("scope", "")})

    # ── curiosities ───────────────────────────────────────
    for r in conn.execute("SELECT * FROM curiosities WHERE status != 'addressed' ORDER BY created_at DESC").fetchall():
        r = dict(r)
        nid = f"memory::cur_{r['id']}"
        extra_nodes.append(
            {
                "id": nid,
                "title": f"❓ {r.get('topic','')[:35]}",
                "type": "curiosity",
                "summary": r.get("reason", ""),
                "tags": [],
            }
        )
        if row:
            extra_edges.append({"from": identity_id, "to": nid, "rel_type": "has_curiosity", "context": ""})

    conn.close()
    return extra_nodes, extra_edges


def build_subgraph(kg, focus_id: str, hops: int) -> tuple[list, list]:
    """특정 노드 중심 서브그래프"""
    from core.db import get_connection
    from collections import deque

    visited = {focus_id}
    queue = deque([(focus_id, 0)])
    nodes = []
    edges = []

    conn = get_connection()
    focus_row = conn.execute("SELECT * FROM kg_nodes WHERE id=?", (focus_id,)).fetchone()
    if focus_row:
        nodes.append(dict(focus_row))

    while queue:
        cur_id, depth = queue.popleft()
        if depth >= hops:
            continue

        for row in conn.execute("SELECT e.*, n.* FROM kg_edges e JOIN kg_nodes n ON e.to_id=n.id WHERE e.from_id=?", (cur_id,)).fetchall():
            nid = row["to_id"]
            edges.append({"from": cur_id, "to": nid, "rel_type": row["rel_type"], "context": row["context"]})
            if nid not in visited:
                visited.add(nid)
                n_row = conn.execute("SELECT * FROM kg_nodes WHERE id=?", (nid,)).fetchone()
                if n_row:
                    nodes.append(dict(n_row))
                queue.append((nid, depth + 1))

        for row in conn.execute("SELECT e.*, n.* FROM kg_edges e JOIN kg_nodes n ON e.from_id=n.id WHERE e.to_id=?", (cur_id,)).fetchall():
            nid = row["from_id"]
            edges.append({"from": nid, "to": cur_id, "rel_type": row["rel_type"], "context": row["context"]})
            if nid not in visited:
                visited.add(nid)
                n_row = conn.execute("SELECT * FROM kg_nodes WHERE id=?", (nid,)).fetchone()
                if n_row:
                    nodes.append(dict(n_row))
                queue.append((nid, depth + 1))

    conn.close()
    return nodes, edges


def render(vault_path: Path, output: Path, focus: str | None = None, hops: int = 2, include_memory: bool = False):
    import json

    initialize_db()
    kg = get_kg()

    if focus:
        focus_node = kg.get_node(focus)
        if not focus_node:
            print(f"❌ 노드 없음: {focus}")
            sys.exit(1)
        nodes, edges = build_subgraph(kg, focus_node["id"], hops)
        title_suffix = f" — '{focus_node['title']}' 중심 {hops}홉"
    else:
        all_nodes, all_edges = kg.dump_graph()
        nodes, edges = all_nodes, all_edges
        title_suffix = " — 전체 그래프"

    if include_memory:
        mem_nodes, mem_edges = load_memory_layer()
        nodes = list(nodes) + mem_nodes
        edges = list(edges) + mem_edges
        title_suffix += " + memory"

    if not nodes:
        print("⚠ 그래프 노드 없음. kg_sync.py를 먼저 실행하세요.")
        sys.exit(1)

    net = Network(
        height="96vh",
        width="100%",
        bgcolor=BG_COLOR,
        font_color=FONT_COLOR,
        directed=True,
    )
    net.set_options(
        f"""
    {{
      "nodes": {{
        "borderWidth": 1,
        "borderWidthSelected": 3,
        "font": {{ "size": 12, "face": "monospace", "color": "{FONT_COLOR}" }},
        "scaling": {{ "min": 10, "max": 40 }}
      }},
      "edges": {{
        "arrows": {{ "to": {{ "enabled": true, "scaleFactor": 0.4 }} }},
        "color": {{ "color": "{EDGE_COLOR}", "highlight": "{EDGE_HIGHLIGHT}", "opacity": 0.7 }},
        "smooth": {{ "type": "continuous", "roundness": 0.2 }},
        "width": 1.2,
        "selectionWidth": 2.5
      }},
      "physics": {{
        "forceAtlas2Based": {{
          "gravitationalConstant": -60,
          "centralGravity": 0.005,
          "springLength": 140,
          "springConstant": 0.06,
          "damping": 0.4
        }},
        "solver": "forceAtlas2Based",
        "stabilization": {{ "iterations": 200 }}
      }},
      "interaction": {{
        "hover": true,
        "tooltipDelay": 80,
        "zoomView": true,
        "navigationButtons": true,
        "keyboard": true
      }}
    }}
    """
    )

    # in-degree 계산
    in_degree = defaultdict(int)
    for e in edges:
        to_key = e.get("to") or e.get("to_id", "")
        in_degree[to_key] += 1

    added = set()
    for n in nodes:
        nid = n["id"]
        if nid in added:
            continue
        added.add(nid)

        ntype = n.get("type", "concept")
        # memory layer 노드는 MEMORY_NODE_COLORS, KG 노드는 NODE_COLORS
        color = MEMORY_NODE_COLORS.get(ntype) or NODE_COLORS.get(ntype, "#95a5a6")

        # identity는 더 크게, 별 모양
        if ntype == "identity":
            size = 40
            shape = "star"
        elif ntype in MEMORY_NODE_COLORS:
            size = 18
            shape = "diamond" if ntype == "directive" else "dot"
        else:
            size = 12 + min(in_degree[nid] * 5, 35)
            shape = "dot"

        # 태그 파싱
        try:
            tags = json.loads(n.get("tags", "[]")) if isinstance(n.get("tags"), str) else (n.get("tags") or [])
        except Exception:
            tags = []

        tooltip = f"<b>{n['title']}</b><br>" f"<i>{ntype}</i><br>" f"{n.get('summary','')[:160]}" + (f"<br>🏷 {', '.join(tags)}" if tags else "")

        net.add_node(
            nid,
            label=n["title"],
            title=tooltip,
            color={"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            size=size,
            shape=shape,
        )

    for e in edges:
        from_key = e.get("from") or e.get("from_id", "")
        to_key = e.get("to") or e.get("to_id", "")
        if from_key not in added or to_key not in added:
            continue
        rtype = e.get("rel_type", "links")
        ecolor = EDGE_COLORS.get(rtype, EDGE_COLOR)
        net.add_edge(
            from_key,
            to_key,
            title=f"{rtype}" + (f": {e['context']}" if e.get("context") else ""),
            color={"color": ecolor, "highlight": EDGE_HIGHLIGHT},
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    net.save_graph(str(output))

    # 범례 + 제목 삽입
    html = output.read_text(encoding="utf-8")
    legend = _build_legend(title_suffix, include_memory)
    html = html.replace("</body>", legend + "\n</body>")
    output.write_text(html, encoding="utf-8")

    unique_edges = set()
    for e in edges:
        fk = e.get("from") or e.get("from_id", "")
        tk = e.get("to") or e.get("to_id", "")
        if fk and tk:
            unique_edges.add((fk, tk))

    print(f"✅ 그래프 저장: {output}")
    print(f"   노드: {len(added)}  엣지: {len(unique_edges)}")


def _build_legend(title_suffix: str, include_memory: bool = False) -> str:
    legend = f"""
<div id="kg-legend" style="
  position:fixed;top:10px;right:10px;
  background:rgba(13,17,23,0.92);
  border:1px solid #30363d;border-radius:10px;
  padding:14px 16px;font-family:monospace;font-size:12px;
  color:#8b949e;z-index:9999;min-width:170px;
  backdrop-filter:blur(6px);">
  <b style="color:#e6edf3;font-size:13px;">🧠 engram graph</b>
  <div style="color:#6e7681;font-size:10px;margin-bottom:8px;">{title_suffix}</div>
  <div style="color:#8b949e;font-size:10px;margin-bottom:4px;">── KG Wiki ──</div>
"""
    for ntype, color in NODE_COLORS.items():
        legend += f'  <div><span style="color:{color};font-size:15px;">●</span> <span style="color:#c9d1d9">{ntype}</span></div>\n'

    if include_memory:
        legend += '  <div style="color:#8b949e;font-size:10px;margin:6px 0 4px;">── Memory DB ──</div>\n'
        shapes = {"identity": "★", "memory": "◆", "directive": "◆", "curiosity": "●"}
        for mtype, color in MEMORY_NODE_COLORS.items():
            sym = shapes.get(mtype, "●")
            legend += f'  <div><span style="color:{color};font-size:15px;">{sym}</span> <span style="color:#c9d1d9">{mtype}</span></div>\n'

    legend += """
  <hr style="border-color:#21262d;margin:8px 0;">
  <div style="font-size:10px;color:#6e7681;">클릭: 노드 선택 | 드래그: 이동 | 스크롤: 줌</div>
</div>"""
    return legend


def main():
    parser = argparse.ArgumentParser(description="Knowledge Graph 시각화")
    parser.add_argument("--vault", default=r"D:\intel_engram", help="vault 루트 경로")
    parser.add_argument("--output", default=None, help="출력 HTML 경로")
    parser.add_argument("--focus", default=None, help="중심 노드 id 또는 제목")
    parser.add_argument("--hops", type=int, default=2, help="focus 모드 탐색 깊이")
    parser.add_argument("--memory", action="store_true", help="memory DB 레이어 포함 (identity/memories/directives/curiosities)")
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    if args.output:
        output = Path(args.output)
    else:
        output = vault / "docs" / "kg_graph.html"

    render(vault, output, args.focus, args.hops, args.memory)

    import os

    try:
        os.startfile(str(output))
    except Exception:
        pass


if __name__ == "__main__":
    main()
