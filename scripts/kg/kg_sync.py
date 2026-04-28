"""
kg_sync.py — vault 마크다운 → Knowledge Graph DB 동기화

Usage:
    python scripts/kg_sync.py [--vault D:\\intel_engram] [--verbose]
"""

import sys
import argparse
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from core.db import initialize_db
from core.knowledge_graph import get_kg


def sync(vault_path: Path, verbose: bool = False):
    docs_dir = vault_path / "docs"
    if not docs_dir.exists():
        print(f"❌ docs 디렉토리 없음: {docs_dir}")
        sys.exit(1)

    initialize_db()
    kg = get_kg()

    md_files = [f for f in docs_dir.rglob("*.md") if "_templates" not in f.parts]
    print(f"🔍 {len(md_files)}개 마크다운 파일 발견: {docs_dir}")

    synced, skipped = 0, 0
    for f in md_files:
        nid = kg.sync_file(f, docs_dir)
        if nid:
            synced += 1
            if verbose:
                print(f"  ✅ {f.relative_to(docs_dir)}  →  {nid}")
        else:
            skipped += 1
            if verbose:
                print(f"  ⚠ 건너뜀: {f.relative_to(docs_dir)}")

    print(f"📦 노드 동기화: {synced}개 완료, {skipped}개 건너뜀")

    print("🔗 wikilink 엣지 재구성 중...")
    kg.resolve_links(docs_dir)

    # 통계
    from core.db import get_connection

    conn = get_connection()
    node_count = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
    conn.close()

    print(f"✨ 완료: 노드 {node_count}개, 엣지 {edge_count}개")
    print(f"   DB: {vault_path / 'engram.db'}")


def main():
    parser = argparse.ArgumentParser(description="Knowledge Graph vault 동기화")
    parser.add_argument("--vault", default=r"D:\intel_engram", help="vault 루트 경로")
    parser.add_argument("--verbose", "-v", action="store_true", help="상세 출력")
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.exists():
        print(f"❌ vault 경로 없음: {vault}")
        sys.exit(1)

    sync(vault, args.verbose)


if __name__ == "__main__":
    main()
