from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from core.dashboard.data_access import parse_tags

_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = Path(__file__).resolve().parent / "assets"
_GRAPH_TEMPLATE = _ASSETS / "graph_widget.html"

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

try:
    from core.graph.knowledge import NODE_COLORS as KG_NODE_COLORS
except Exception:
    KG_NODE_COLORS = NODE_COLORS_FALLBACK


def _md_to_html(text: str) -> str:
    text = re.sub(r"```[\w]*\n?", "", text)
    text = re.sub(r"`([^`]+)`", r'<code style="background:#161b22;padding:1px 4px;border-radius:3px;font-size:11px">​\1</code>', text)
    text = re.sub(r"^### (.+)$", r'<strong style="color:#79c0ff;font-size:11px">▸ \1</strong>', text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r'<strong style="color:#58a6ff;font-size:12px">▸ \1</strong>', text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r'<strong style="color:#58a6ff;font-size:13px">▸ \1</strong>', text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"^\s*[-*] (.+)$", r'<span style="color:#8b949e">•</span> \1', text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", r'<hr style="border:none;border-top:1px solid #30363d;margin:4px 0">', text, flags=re.MULTILINE)
    text = text.replace("\n", "<br>")
    return text


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
    vis_js_path = _ROOT / "lib" / "vis-9.1.2" / "vis-network.min.js"
    vis_css_path = _ROOT / "lib" / "vis-9.1.2" / "vis-network.css"
    vis_js = vis_js_path.read_text(encoding="utf-8") if vis_js_path.exists() else ""
    vis_css = vis_css_path.read_text(encoding="utf-8") if vis_css_path.exists() else ""
    graph_css = (_ASSETS / "graph_widget.css").read_text(encoding="utf-8")
    graph_js_body = (_ASSETS / "graph_widget.js").read_text(encoding="utf-8")

    in_degree: dict[str, int] = defaultdict(int)
    for e in edges:
        in_degree[e.get("to") or e.get("to_id", "")] += 1

    vis_nodes: list[dict] = []
    added: set[str] = set()
    for n in nodes:
        nid = n["id"]
        if nid in added:
            continue
        added.add(nid)
        ntype = n.get("type", "concept")
        color = MEMORY_NODE_COLORS.get(ntype) or KG_NODE_COLORS.get(ntype, "#95a5a6")
        if ntype == "identity":
            base_size, shape = 40, "star"
        elif ntype in MEMORY_NODE_COLORS:
            base_size, shape = 18, "diamond"
        else:
            base_size = 12 + min(in_degree[nid] * 5, 35)
            shape = "dot"
        size = int(base_size * size_scale)
        tags = parse_tags(n.get("tags"))
        summary_html = _md_to_html(n.get("summary", "")[:400])
        tooltip = (
            f'<div class="eg-tip">'
            f'<div class="eg-tip-title">{n["title"]}</div>'
            f'<div class="eg-tip-type">{ntype}</div>'
            f'<div class="eg-tip-body">{summary_html}</div>' + (f'<div class="eg-tip-tags">🏷 {", ".join(tags)}</div>' if tags else "") + "</div>"
        )
        vis_nodes.append(
            {
                "id": nid,
                "label": n["title"],
                "_tooltip": tooltip,
                "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
                "size": size,
                "_baseSize": base_size,
                "shape": shape,
                "font": {"color": "#c9d1d9"},
            }
        )

    vis_edges: list[dict] = []
    for i, e in enumerate(edges):
        fk = e.get("from") or e.get("from_id", "")
        tk = e.get("to") or e.get("to_id", "")
        if fk not in added or tk not in added:
            continue
        rtype = e.get("rel_type", "links")
        ecolor = EDGE_COLORS.get(rtype, "#30363d")
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

    phys_checked = "checked" if physics_enabled else ""
    abs_grav = abs(grav_const)
    ss_init = int(size_scale * 10)

    template = _GRAPH_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__VIS_CSS__": vis_css,
        "__GRAPH_CSS__": graph_css,
        "__VIS_JS__": vis_js,
        "__PHYS_CHECKED__": phys_checked,
        "__ABS_GRAV__": str(abs_grav),
        "__SPRING_LENGTH__": str(spring_length),
        "__SIZE_SCALE__": f"{size_scale:.1f}",
        "__SS_INIT__": str(ss_init),
        "__NODES_JSON__": nodes_json,
        "__EDGES_JSON__": edges_json,
        "__HEIGHT__": str(height),
        "__PHYSICS_ENABLED__": str(physics_enabled).lower(),
        "__GRAV_CONST__": str(grav_const),
        "__CENTRAL_GRAVITY__": str(central_gravity),
        "__SPRING_CONST__": str(spring_const),
        "__DAMPING__": str(damping),
        "__GRAPH_JS_BODY__": graph_js_body,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template

