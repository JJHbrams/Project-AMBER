"""
KG 노드 ID 변경 스크립트 (1회성 진짜 마이그레이션)

수행 작업:
1. SQLite kg_nodes.id 변경 (FK OFF 상태에서 UPDATE)
2. SQLite kg_edges from_id / to_id 업데이트
3. KuzuDB KGNode 삭제 → 동일 속성으로 재생성 + KG_EDGE 재연결
4. 마크다운 frontmatter id 필드 수정
5. ~/.engram/user.config.yaml의 kg_node_map에서 해당 매핑 제거
   (rename 후에는 heuristic 자동 매칭으로 충분)

실행 방법:
  python scripts/dev/kg_rename_node.py [--dry-run]

완료 후 MCP 서버 재시작 필요.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

DRY_RUN = "--dry-run" in sys.argv

OLD_ID = "iccc-for-arona"
NEW_ID = "project-engram"
# project_key 'project-engram-08dcaca0' → normalize → 'projectengram'
# node_id 'project-engram'              → normalize → 'projectengram'  → heuristic match ✓


# ── 경로 ─────────────────────────────────────────────────────────────────────

def _load_db_path() -> Path:
    cfg = Path.home() / ".engram" / "user.config.yaml"
    with open(cfg, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    root = data.get("db", {}).get("root_dir", "D:/intel_engram")
    return Path(root) / "engram.db"


def _load_vault_path() -> Path:
    cfg = Path.home() / ".engram" / "user.config.yaml"
    with open(cfg, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    root = data.get("db", {}).get("root_dir", "D:/intel_engram")
    return Path(root) / "docs"


# ── 1. SQLite ─────────────────────────────────────────────────────────────────

def _migrate_sqlite(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 노드 존재 확인
    row = conn.execute("SELECT id, title, path FROM kg_nodes WHERE id=?", (OLD_ID,)).fetchone()
    if not row:
        conn.close()
        return {"status": "skip", "reason": f"'{OLD_ID}' not found in kg_nodes"}

    # 이미 새 ID 존재 여부
    existing_new = conn.execute("SELECT id FROM kg_nodes WHERE id=?", (NEW_ID,)).fetchone()
    if existing_new:
        conn.close()
        return {"status": "skip", "reason": f"'{NEW_ID}' already exists"}

    md_path = row["path"]
    edge_count_from = conn.execute("SELECT count(*) FROM kg_edges WHERE from_id=?", (OLD_ID,)).fetchone()[0]
    edge_count_to   = conn.execute("SELECT count(*) FROM kg_edges WHERE to_id=?",   (OLD_ID,)).fetchone()[0]

    print(f"  node  : '{OLD_ID}' → '{NEW_ID}'")
    print(f"  edges : from={edge_count_from}, to={edge_count_to}")
    print(f"  md    : {md_path}")

    if not DRY_RUN:
        # FK 끄고 PK 변경 (SQLite UPDATE는 PK 변경 허용, FK는 일시 OFF 필요)
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("UPDATE kg_nodes SET id=? WHERE id=?", (NEW_ID, OLD_ID))
            conn.execute("UPDATE kg_edges SET from_id=? WHERE from_id=?", (NEW_ID, OLD_ID))
            conn.execute("UPDATE kg_edges SET to_id=? WHERE to_id=?",   (NEW_ID, OLD_ID))
        conn.execute("PRAGMA foreign_keys=ON")
        print("  [done] SQLite 업데이트 완료")
    else:
        print("  [dry]  SQLite 업데이트 스킵")

    conn.close()
    return {"status": "ok", "md_path": md_path, "edge_from": edge_count_from, "edge_to": edge_count_to}


# ── 2. KuzuDB ─────────────────────────────────────────────────────────────────

def _migrate_kuzudb(db_path: Path) -> str:
    """KuzuDB KGNode ID 변경: 기존 속성 읽기 → 삭제 → 새 노드 생성 → 엣지 재연결
    
    MCP 서버 실행 중에는 KuzuDB 잠금으로 접근 불가.
    이 경우 graceful skip 후 서버 재시작 + kg_sync + 수동 정리 안내.
    """
    kuzu_dir = db_path.parent / "semantic_graph"
    if not kuzu_dir.exists():
        return "skip: KuzuDB dir not found"

    try:
        import kuzu
    except ImportError:
        return "skip: kuzu not installed"

    try:
        db = kuzu.Database(str(kuzu_dir))
        conn = kuzu.Connection(db)
    except RuntimeError as e:
        if "lock" in str(e).lower() or "Could not set lock" in str(e):
            return (
                "SKIP (잠금): MCP 서버가 KuzuDB를 점유 중.\n"
                "  → MCP 서버 재시작 후 kg_sync 실행하면 new 'project-engram' 노드가 생성됨.\n"
                "  → 그 후 old 'iccc-for-arona' 노드는 KG Cypher로 수동 삭제:\n"
                "     MATCH (n:KGNode {id: 'iccc-for-arona'}) DETACH DELETE n"
            )
        raise

    # 기존 노드 속성 읽기
    res = conn.execute(
        "MATCH (n:KGNode {id: $id}) RETURN n.title, n.type, n.tags, n.summary, n.embedding, n.content_hash, n.updated_at",
        {"id": OLD_ID},
    )
    if not res.has_next():
        return f"skip: '{OLD_ID}' not found in KuzuDB"

    row = res.get_next()
    props = {
        "id": NEW_ID,
        "title": row[0] or "",
        "type": row[1] or "project",
        "tags": row[2] or "",
        "summary": row[3] or "",
        "embedding": row[4] or "",
        "content_hash": row[5] or "",
        "updated_at": row[6] or "",
    }

    # 연결된 KG_EDGE 저장 (old_id 관련)
    out_edges, in_edges = [], []
    res_out = conn.execute(
        "MATCH (a:KGNode {id: $id})-[e:KG_EDGE]->(b:KGNode) RETURN b.id, e.rel_type, e.weight",
        {"id": OLD_ID},
    )
    while res_out.has_next():
        r = res_out.get_next()
        out_edges.append({"to": r[0], "rel": r[1], "w": r[2]})

    res_in = conn.execute(
        "MATCH (a:KGNode)-[e:KG_EDGE]->(b:KGNode {id: $id}) RETURN a.id, e.rel_type, e.weight",
        {"id": OLD_ID},
    )
    while res_in.has_next():
        r = res_in.get_next()
        in_edges.append({"from": r[0], "rel": r[1], "w": r[2]})

    print(f"  KuzuDB: 속성={list(props.keys())}, out_edges={len(out_edges)}, in_edges={len(in_edges)}")

    if DRY_RUN:
        return "dry: KuzuDB 업데이트 스킵"

    # 기존 노드 삭제 (연결 엣지 포함 자동 삭제)
    conn.execute("MATCH (n:KGNode {id: $id}) DETACH DELETE n", {"id": OLD_ID})

    # 새 노드 생성
    conn.execute(
        "CREATE (n:KGNode {id: $id, title: $title, type: $type, tags: $tags, "
        "summary: $summary, embedding: $embedding, content_hash: $hash, updated_at: $ts})",
        {
            "id": props["id"], "title": props["title"], "type": props["type"],
            "tags": props["tags"], "summary": props["summary"],
            "embedding": props["embedding"], "hash": props["content_hash"],
            "ts": props["updated_at"],
        },
    )

    # 엣지 재연결
    for e in out_edges:
        try:
            conn.execute(
                "MATCH (a:KGNode {id: $fid}), (b:KGNode {id: $tid}) "
                "CREATE (a)-[:KG_EDGE {rel_type: $rel, weight: $w}]->(b)",
                {"fid": NEW_ID, "tid": e["to"], "rel": e["rel"], "w": e["w"]},
            )
        except Exception as ex:
            print(f"    [warn] edge {NEW_ID}→{e['to']}: {ex}")

    for e in in_edges:
        try:
            conn.execute(
                "MATCH (a:KGNode {id: $fid}), (b:KGNode {id: $tid}) "
                "CREATE (a)-[:KG_EDGE {rel_type: $rel, weight: $w}]->(b)",
                {"fid": e["from"], "tid": NEW_ID, "rel": e["rel"], "w": e["w"]},
            )
        except Exception as ex:
            print(f"    [warn] edge {e['from']}→{NEW_ID}: {ex}")

    return f"done: '{OLD_ID}' → '{NEW_ID}', out={len(out_edges)}, in={len(in_edges)}"


# ── 3. 마크다운 frontmatter ───────────────────────────────────────────────────

def _migrate_markdown(vault_path: Path, md_rel_path: str) -> str:
    md_file = vault_path / md_rel_path
    if not md_file.exists():
        return f"skip: {md_file} not found"

    content = md_file.read_text(encoding="utf-8")

    # frontmatter id 필드 교체 (정확히 'id: iccc-for-arona' 패턴만)
    new_content = re.sub(
        r"^(id:\s*)" + re.escape(OLD_ID) + r"\s*$",
        f"\\g<1>{NEW_ID}",
        content,
        flags=re.MULTILINE,
    )

    if new_content == content:
        return f"skip: frontmatter id '{OLD_ID}' not found in {md_file.name}"

    if not DRY_RUN:
        md_file.write_text(new_content, encoding="utf-8")
        return f"done: {md_file}"
    return f"dry: {md_file}"


# ── 4. user.config.yaml 매핑 정리 ────────────────────────────────────────────

def _cleanup_user_config() -> str:
    cfg_path = Path.home() / ".engram" / "user.config.yaml"
    if not cfg_path.exists():
        return "skip: user.config.yaml not found"

    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    mapping = data.get("memory", {}).get("scope", {}).get("kg_node_map", {})
    removed = []
    for k, v in list(mapping.items()):
        if v == OLD_ID:
            removed.append(k)
            del mapping[k]

    if not removed:
        return "skip: no entries pointing to OLD_ID in kg_node_map"

    if not DRY_RUN:
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return f"done: removed {removed}"
    return f"dry: would remove {removed}"


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"{'[DRY-RUN] ' if DRY_RUN else ''}KG 노드 ID 변경: '{OLD_ID}' → '{NEW_ID}'\n")

    db_path = _load_db_path()
    vault_path = _load_vault_path()
    print(f"DB   : {db_path}")
    print(f"Vault: {vault_path}\n")

    print("[1] SQLite kg_nodes / kg_edges")
    result = _migrate_sqlite(db_path)
    if result["status"] == "skip":
        print(f"  [skip] {result['reason']}")
        return

    print(f"\n[2] KuzuDB KGNode")
    kuzu_result = _migrate_kuzudb(db_path)
    print(f"  {kuzu_result}")

    print(f"\n[3] 마크다운 frontmatter")
    md_result = _migrate_markdown(vault_path, result["md_path"])
    print(f"  {md_result}")

    print(f"\n[4] user.config.yaml kg_node_map 정리")
    cfg_result = _cleanup_user_config()
    print(f"  {cfg_result}")

    print("\n--- 완료 ---")
    if not DRY_RUN:
        print("MCP 서버 재시작 후 kg_sync 실행 권장 (KuzuDB 캐시 무효화)")


if __name__ == "__main__":
    main()
