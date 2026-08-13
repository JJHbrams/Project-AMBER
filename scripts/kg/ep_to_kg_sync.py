"""
ep_to_kg_sync.py — memories → EpisodeNode 백필 + EP_TO_KG 연결 배치 소급 동기화

MCP 서버(포트 17385)가 중단된 상태에서 실행해야 한다.
KuzuDB는 단일 writer 제약이 있어 MCP 서버와 동시 사용 불가.

사용법:
    conda activate intel_engram
    python scripts/kg/ep_to_kg_sync.py [--threshold 0.55] [--top-k 3]
    python scripts/kg/ep_to_kg_sync.py --sync-memories          # SQLite memories → EpisodeNode 백필 후 EP_TO_KG 연결
    python scripts/kg/ep_to_kg_sync.py --reconcile              # 무결성 dry-run
    python scripts/kg/ep_to_kg_sync.py --apply-reconcile        # stale EpisodeNode 명시적 삭제
"""

import argparse
import asyncio
import json
import sys
import logging
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.graph.semantic import get_semantic_graph

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def backfill_memories(sg) -> dict:
    """SQLite memories 테이블 전체를 KuzuDB EpisodeNode로 백필.
    이미 존재하는 EpisodeNode는 upsert이므로 중복 없이 안전하게 재실행 가능.
    upsert_episode() 내부에서 EP_TO_KG 자동 연결까지 수행한다.
    """
    from core.storage.db import get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT id, session_id, content, keywords, created_at FROM memories ORDER BY id"
    ).fetchall()
    conn.close()

    total = len(rows)
    success = 0
    failed = 0
    print(f"\nSQLite memories: {total}개 → EpisodeNode 백필 시작 ...")
    for row in rows:
        ep_id = str(row[0])
        session_id = str(row[1] or "")
        content = row[2] or ""
        keywords = row[3] or ""
        created_at = row[4] or ""
        ok = await sg.upsert_episode(
            episode_id=ep_id,
            content=content,
            keywords=keywords,
            session_id=session_id,
            created_at=created_at,
        )
        if ok:
            success += 1
        else:
            failed += 1
        if success % 50 == 0 and success > 0:
            print(f"  ... {success}/{total} 완료")

    print(f"백필 완료: 성공={success}, 실패={failed}")
    return {"total": total, "success": success, "failed": failed}


def load_canonical_memory_ids() -> list[str]:
    from core.storage.db import get_connection

    conn = get_connection()
    try:
        return [str(row[0]) for row in conn.execute("SELECT id FROM memories").fetchall()]
    finally:
        conn.close()


async def _main_async(args) -> None:
    sg = get_semantic_graph()

    if not sg.enabled:
        logger.error("SemanticGraph 비활성화 — KuzuDB 접근 불가. MCP 서버가 실행 중이면 중단 후 재시도.")
        sys.exit(1)

    if args.reconcile or args.apply_reconcile:
        report = await sg.reconcile_episodes(
            load_canonical_memory_ids,
            apply=args.apply_reconcile,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # 현황 확인
    ep_count = await sg.count("MATCH (e:EpisodeNode) RETURN COUNT(e)")
    kg_count = await sg.count("MATCH (k:KGNode) RETURN COUNT(k)")
    link_count = await sg.count("MATCH ()-[r:EP_TO_KG]->() RETURN COUNT(r)")

    print(f"\n현황: EpisodeNode={ep_count}, KGNode={kg_count}, 기존 EP_TO_KG={link_count}")

    if args.dry_run:
        print("(dry-run 모드: 변경 없음)")
        return

    # 1단계: SQLite memories → EpisodeNode 백필 (--sync-memories 또는 EpisodeNode가 비어있을 때)
    if args.sync_memories or ep_count == 0:
        await backfill_memories(sg)
        # 백필 후 현황 재조회
        ep_count = await sg.count("MATCH (e:EpisodeNode) RETURN COUNT(e)")
        print(f"백필 후 EpisodeNode: {ep_count}개")

    if ep_count == 0:
        print("처리할 에피소드 없음.")
        return

    # 2단계: EP_TO_KG 소급 연결 (--sync-memories는 upsert_episode 내부에서 이미 수행하지만
    #         KGNode가 sync_from_kg 이전에 없었을 수 있으므로 한 번 더 전체 적용)
    from core.config.runtime_config import get_cfg_value

    effective_threshold = (
        float(args.threshold)
        if args.threshold is not None
        else float(get_cfg_value("memory.ep_to_kg.semantic_threshold", 0.55))
    )
    print(f"\nEP_TO_KG 소급 연결 시작 (threshold={effective_threshold}, top_k={args.top_k}) ...")
    result = await sg.sync_all_ep_to_kg(sem_threshold=effective_threshold, top_k=args.top_k)

    link_after = await sg.count("MATCH ()-[r:EP_TO_KG]->() RETURN COUNT(r)")

    print(f"\n완료: 처리={result['processed']}, 신규 생성={result['linked']}")
    print(f"EP_TO_KG 총 릴레이션: {link_count} → {link_after}")


def main() -> None:
    parser = argparse.ArgumentParser(description="memories 백필 + EP_TO_KG 배치 소급 동기화")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="시맨틱 유사도 임계값 (기본: config memory.ep_to_kg.semantic_threshold)",
    )
    parser.add_argument("--top-k", type=int, default=3, help="에피소드당 연결할 최대 KGNode 수 (기본 3)")
    parser.add_argument("--dry-run", action="store_true", help="실제 연결 없이 현황만 출력")
    parser.add_argument("--sync-memories", action="store_true", help="SQLite memories → EpisodeNode 백필 후 EP_TO_KG 연결")
    reconcile_group = parser.add_mutually_exclusive_group()
    reconcile_group.add_argument("--reconcile", action="store_true", help="SQLite 기준 EpisodeNode 무결성 dry-run")
    reconcile_group.add_argument("--apply-reconcile", action="store_true", help="SQLite에 없는 EpisodeNode를 명시적으로 삭제")
    args = parser.parse_args()
    if (args.reconcile or args.apply_reconcile) and (args.dry_run or args.sync_memories):
        parser.error("reconcile options cannot be combined with --dry-run or --sync-memories")
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
