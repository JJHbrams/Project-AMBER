"""
callgraph.py — engram 프로젝트 Python 호출 그래프 시각화
AST 정적 분석 + pyvis 인터랙티브 HTML

Usage:
    python scripts/callgraph.py [--output docs/callgraph.html] [--root .]
"""

import ast
import sys
import argparse
from pathlib import Path
from collections import defaultdict
from pyvis.network import Network

# 모듈별 색상 (옵시디언 그래프뷰 느낌)
MODULE_COLORS = {
    "core":        "#7c6af7",  # 보라
    "overlay":     "#4ecdc4",  # 청록
    "discord_bot": "#f9ca24",  # 노랑
    "mcp_server":  "#f0932b",  # 주황
    "engram":      "#eb4d4b",  # 빨강
    "test":        "#6ab04c",  # 초록
    "__other__":   "#95a5a6",  # 회색
}

SKIP_DIRS = {"__pycache__", ".git", "build", "dist", ".venv", "node_modules"}


def get_module_color(module_key: str) -> str:
    for prefix, color in MODULE_COLORS.items():
        if module_key.startswith(prefix):
            return color
    return MODULE_COLORS["__other__"]


def module_key(filepath: Path, root: Path) -> str:
    rel = filepath.relative_to(root)
    parts = rel.with_suffix("").parts
    return ".".join(parts)


def collect_definitions(tree: ast.AST, mod_key: str) -> dict[str, str]:
    """함수/메서드 정의 수집 → {qualified_name: mod_key}"""
    defs = {}
    class_stack = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node):
            if class_stack:
                qname = f"{mod_key}.{class_stack[-1]}.{node.name}"
            else:
                qname = f"{mod_key}.{node.name}"
            defs[qname] = mod_key
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return defs


def collect_calls(tree: ast.AST, mod_key: str, all_defs: dict) -> list[tuple[str, str]]:
    """함수 내부 호출 관계 수집 → [(caller_qname, callee_short)]"""
    edges = []
    class_stack = []
    func_stack = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node):
            if class_stack:
                qname = f"{mod_key}.{class_stack[-1]}.{node.name}"
            else:
                qname = f"{mod_key}.{node.name}"
            func_stack.append(qname)
            self.generic_visit(node)
            func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if not func_stack:
                self.generic_visit(node)
                return

            caller = func_stack[-1]
            callee = None

            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr

            if callee:
                # 프로젝트 내 정의된 함수인지 매칭
                for qname in all_defs:
                    if qname.endswith(f".{callee}") or qname == callee:
                        edges.append((caller, qname))
                        break
                else:
                    # 매칭 안 돼도 외부 호출로 추가 (선택적)
                    pass

            self.generic_visit(node)

    Visitor().visit(tree)
    return edges


def build_graph(root: Path) -> tuple[dict, list]:
    all_defs = {}  # qname → mod_key
    all_edges = []

    py_files = [
        p for p in root.rglob("*.py")
        if not any(skip in p.parts for skip in SKIP_DIRS)
    ]

    # 1패스: 정의 수집
    file_trees = {}
    for f in py_files:
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src, filename=str(f))
            mk = module_key(f, root)
            file_trees[mk] = tree
            all_defs.update(collect_definitions(tree, mk))
        except SyntaxError:
            print(f"  ⚠ syntax error: {f}", file=sys.stderr)

    # 2패스: 호출 수집
    for mk, tree in file_trees.items():
        edges = collect_calls(tree, mk, all_defs)
        all_edges.extend(edges)

    return all_defs, all_edges


def render_html(all_defs: dict, all_edges: list, output: Path):
    net = Network(
        height="95vh",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="#e0e0e0",
        directed=True,
    )
    net.set_options("""
    {
      "nodes": {
        "borderWidth": 1,
        "borderWidthSelected": 3,
        "size": 14,
        "font": { "size": 11, "face": "monospace" }
      },
      "edges": {
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.5 } },
        "color": { "color": "#444466", "highlight": "#a29bfe" },
        "smooth": { "type": "continuous" },
        "width": 1
      },
      "physics": {
        "forceAtlas2Based": {
          "gravitationalConstant": -80,
          "centralGravity": 0.01,
          "springLength": 120,
          "springConstant": 0.08
        },
        "solver": "forceAtlas2Based",
        "stabilization": { "iterations": 150 }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "zoomView": true,
        "navigationButtons": true
      }
    }
    """)

    added_nodes = set()

    def add_node(qname, mod_key_val):
        if qname in added_nodes:
            return
        short = qname.split(".")[-1]
        color = get_module_color(mod_key_val)
        # in-degree로 크기 조절 (나중에)
        net.add_node(
            qname,
            label=short,
            title=qname,
            color=color,
            shape="dot",
        )
        added_nodes.add(qname)

    # 정의된 모든 노드 추가
    for qname, mk in all_defs.items():
        add_node(qname, mk)

    # 엣지 추가 + 크기 조절용 in-degree 카운트
    in_degree = defaultdict(int)
    valid_edges = []
    for src, dst in all_edges:
        if src in all_defs and dst in all_defs and src != dst:
            valid_edges.append((src, dst))
            in_degree[dst] += 1

    # in-degree 기반 노드 크기 재설정
    for qname in added_nodes:
        size = 10 + min(in_degree[qname] * 4, 30)
        net.get_node(qname)["size"] = size

    edge_set = set()
    for src, dst in valid_edges:
        key = (src, dst)
        if key not in edge_set:
            net.add_edge(src, dst)
            edge_set.add(key)

    # 범례 주석 (HTML에 직접 삽입)
    output.parent.mkdir(parents=True, exist_ok=True)
    net.save_graph(str(output))

    # 범례 패치
    html = output.read_text(encoding="utf-8")
    legend_html = """
<div style="position:fixed;top:10px;right:10px;background:#16213e;border:1px solid #444;border-radius:8px;padding:12px;font-family:monospace;font-size:12px;color:#ccc;z-index:9999;">
  <b style="color:#fff">engram Call Graph</b><br><br>
"""
    for mod, color in MODULE_COLORS.items():
        if mod == "__other__":
            continue
        legend_html += f'  <span style="color:{color}">●</span> {mod}<br>\n'
    legend_html += "</div>"

    html = html.replace("</body>", legend_html + "\n</body>")
    output.write_text(html, encoding="utf-8")

    print(f"✅ 그래프 저장됨: {output}")
    print(f"   노드: {len(added_nodes)}  엣지: {len(edge_set)}")


def main():
    parser = argparse.ArgumentParser(description="engram Call Graph visualizer")
    parser.add_argument("--root", default=".", help="프로젝트 루트 경로")
    parser.add_argument("--output", default="docs/callgraph.html", help="출력 HTML 경로")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output

    print(f"🔍 분석 중: {root}")
    all_defs, all_edges = build_graph(root)
    print(f"   함수 정의: {len(all_defs)}개  호출 관계: {len(all_edges)}개")
    render_html(all_defs, all_edges, output)


if __name__ == "__main__":
    main()
