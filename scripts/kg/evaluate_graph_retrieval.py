"""운영 KuzuDB에서 graph-aware retrieval golden query를 평가한다.

KuzuDB writer인 MCP 서버를 중단한 상태에서 실행한다.

Usage:
    python scripts/kg/evaluate_graph_retrieval.py
    python scripts/kg/evaluate_graph_retrieval.py --golden path/to/cases.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.graph.semantic.semantic_graph import SemanticGraph
from core.memory.store import search_memory_hits


async def evaluate_case(sg: SemanticGraph, case: dict) -> dict:
    query = str(case.get("query", "") or "").strip()
    if not query:
        return {
            "name": str(case.get("name", "unnamed")),
            "passed": False,
            "error": "query is empty",
        }

    query_vec = await sg.compute_embedding(query)
    if not query_vec:
        return {
            "name": str(case.get("name", query)),
            "query": query,
            "passed": False,
            "error": "embedding failed",
        }

    episode_hits = await search_memory_hits(
        query,
        limit=int(case.get("episode_limit", 5)),
        project_key=str(case.get("project_key", "") or ""),
        query_vec=query_vec,
        semantic_graph=sg,
    )
    graph_hits = await sg.graph_retrieve_from_episodes(
        episode_hits,
        top_k=int(case.get("graph_limit", 8)),
    )
    episode_ids = [str(hit.get("id", "")) for hit in episode_hits]
    graph_ids = [str(hit.get("id", "")) for hit in graph_hits]

    checks: dict[str, bool] = {}
    expected_episode_ids = {
        str(value) for value in case.get("expected_episode_ids_any", [])
    }
    if expected_episode_ids:
        checks["expected_episode_ids_any"] = bool(expected_episode_ids & set(episode_ids))

    expected_kg_ids = {str(value) for value in case.get("expected_kg_ids_any", [])}
    if expected_kg_ids:
        checks["expected_kg_ids_any"] = bool(expected_kg_ids & set(graph_ids))

    if case.get("forbid_episode_hits"):
        checks["forbid_episode_hits"] = not episode_hits
    if case.get("forbid_graph_hits"):
        checks["forbid_graph_hits"] = not graph_hits
    if not checks:
        checks["has_graph_hits"] = bool(graph_hits)

    return {
        "name": str(case.get("name", query)),
        "query": query,
        "passed": all(checks.values()),
        "checks": checks,
        "episode_ids": episode_ids,
        "graph_ids": graph_ids,
        "graph_scores": {
            str(hit.get("id", "")): float(hit.get("score", 0.0))
            for hit in graph_hits
        },
    }


async def _main_async(golden_path: Path) -> int:
    payload = json.loads(golden_path.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No golden cases found in {golden_path}")

    sg = SemanticGraph(read_only=True)
    if not sg.enabled:
        raise RuntimeError(
            "SemanticGraph is unavailable. Stop the MCP writer and confirm the KuzuDB path."
        )
    try:
        results = [await evaluate_case(sg, case) for case in cases]
    finally:
        sg.async_conn.close()

    report = {
        "golden_file": str(golden_path),
        "passed": all(result["passed"] for result in results),
        "passed_count": sum(1 for result in results if result["passed"]),
        "total_count": len(results),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate graph-aware retrieval golden queries")
    parser.add_argument(
        "--golden",
        type=Path,
        default=ROOT / "config" / "evaluation" / "graph_retrieval_golden.json",
        help="Golden query JSON path",
    )
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main_async(args.golden.resolve()))
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
