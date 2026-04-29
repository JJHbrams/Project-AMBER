"""
kg_lint.py — Wiki 품질 점검 도구

점검 항목:
  1. frontmatter 필드 누락 (title, note_type, tags)
  2. summary 없는 DB 노드
  3. _inbox/ 에 체류 중인 노트 (모두 경고)
  4. 고립 노드 — 인바운드·아웃바운드 엣지 없고 [[링크]]도 없는 노트
  5. 빈/너무 짧은 노트 (본문 200자 미만)
  6. 제목 중복 노드

Usage:
    python scripts/kg_lint.py [--fix-summary] [-v]
"""

import sys
import re
import argparse
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from core.storage.db import initialize_db
from core.graph.knowledge import get_kg
from core.config.runtime_config import get_db_root_dir

REQUIRED_FM_FIELDS = {"title", "note_type", "tags"}
# 기존 파일은 "type:" 키를 사용 — 두 가지 모두 허용
_NOTE_TYPE_ALIASES = {"note_type", "type"}
MIN_BODY_CHARS = 200


# ── 유틸 ─────────────────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict:
    """최상단 YAML 프론트매터를 파싱한다."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    result = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def _extract_wikilinks(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def _body_length(text: str) -> int:
    """frontmatter 제거 후 본문 길이."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return len(text[end + 4 :].strip())
    return len(text.strip())


# ── 점검 함수들 ───────────────────────────────────────────────────────────────


def check_frontmatter(vault: Path) -> list[dict]:
    issues = []
    for md in vault.rglob("*.md"):
        if "_templates" in md.parts:
            continue
        fm = _parse_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
        fm_keys = set(fm.keys())
        # note_type 또는 type 중 하나라도 있으면 통과
        if _NOTE_TYPE_ALIASES & fm_keys:
            fm_keys = (fm_keys - _NOTE_TYPE_ALIASES) | {"note_type"}
        required = {"title", "note_type", "tags"}
        missing = required - fm_keys
        if missing:
            issues.append(
                {
                    "file": str(md.relative_to(vault)),
                    "issue": "frontmatter 필드 누락",
                    "detail": f"누락: {', '.join(sorted(missing))}",
                }
            )
    return issues


def check_inbox(vault: Path) -> list[dict]:
    """_inbox/ 에 있는 노트는 모두 미정제 경고."""
    issues = []
    inbox = vault / "_inbox"
    if not inbox.exists():
        return []
    for md in inbox.rglob("*.md"):
        issues.append(
            {
                "file": str(md.relative_to(vault)),
                "issue": "_inbox 체류",
                "detail": "정제 후 적절한 디렉토리로 이동 필요",
            }
        )
    return issues


def check_empty_notes(vault: Path) -> list[dict]:
    issues = []
    for md in vault.rglob("*.md"):
        if "_templates" in md.parts:
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        if _body_length(text) < MIN_BODY_CHARS:
            issues.append(
                {
                    "file": str(md.relative_to(vault)),
                    "issue": "본문 부족",
                    "detail": f"본문 {_body_length(text)}자 (최소 {MIN_BODY_CHARS}자)",
                }
            )
    return issues


def check_isolated_nodes(vault: Path) -> list[dict]:
    """엣지가 없고 [[위키링크]]도 없는 노드."""
    kg = get_kg()
    all_nodes = kg.list_nodes(limit=500)
    issues = []
    for node in all_nodes:
        edges = kg.get_edges(node["id"])
        if edges:
            continue
        rel_path = node.get("path", "")
        abs_path = vault / rel_path
        if not abs_path.exists():
            continue
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        links = _extract_wikilinks(text)
        if not links:
            issues.append(
                {
                    "file": rel_path,
                    "issue": "고립 노드",
                    "detail": "인바운드·아웃바운드 엣지 없음, [[링크]]도 없음",
                }
            )
    return issues


def check_missing_summary(fix: bool = False) -> list[dict]:
    """DB에서 summary가 없는 노드."""
    kg = get_kg()
    all_nodes = kg.list_nodes(limit=500)
    issues = []
    for node in all_nodes:
        if not node.get("summary", "").strip():
            issues.append(
                {
                    "file": node.get("path", node["id"]),
                    "issue": "summary 없음",
                    "detail": "kg_update_node() 또는 kg_sync()로 갱신 필요",
                }
            )
    return issues


def check_duplicate_titles() -> list[dict]:
    kg = get_kg()
    all_nodes = kg.list_nodes(limit=500)
    seen: dict[str, list[str]] = {}
    for node in all_nodes:
        t = node.get("title", "").lower().strip()
        if t:
            seen.setdefault(t, []).append(node["id"])
    issues = []
    for title, ids in seen.items():
        if len(ids) > 1:
            issues.append(
                {
                    "file": ", ".join(ids),
                    "issue": "제목 중복",
                    "detail": f"'{title}' — {len(ids)}개 노드",
                }
            )
    return issues


# ── 메인 ─────────────────────────────────────────────────────────────────────


def run_lint(vault: Path, fix_summary: bool = False, verbose: bool = False) -> dict:
    initialize_db()
    docs = vault / "docs"
    if not docs.exists():
        docs = vault  # fallback

    results: dict[str, list[dict]] = {}

    checks = [
        ("frontmatter", lambda: check_frontmatter(docs)),
        ("inbox", lambda: check_inbox(docs)),
        ("empty_notes", lambda: check_empty_notes(docs)),
        ("isolated_nodes", lambda: check_isolated_nodes(docs)),
        ("missing_summary", lambda: check_missing_summary(fix=fix_summary)),
        ("duplicate_titles", lambda: check_duplicate_titles()),
    ]

    total = 0
    for name, fn in checks:
        issues = fn()
        results[name] = issues
        total += len(issues)
        if verbose or issues:
            label = {
                "frontmatter": "frontmatter 누락",
                "inbox": "_inbox 체류",
                "empty_notes": "본문 부족",
                "isolated_nodes": "고립 노드",
                "missing_summary": "summary 없음",
                "duplicate_titles": "제목 중복",
            }[name]
            status = "✅" if not issues else "⚠️ "
            print(f"{status} {label}: {len(issues)}건")
            if verbose:
                for i in issues:
                    print(f"   {i['file']}")
                    print(f"   → {i['detail']}")

    print(f"\n{'✅ 이상 없음' if total == 0 else f'⚠️  총 {total}건 이슈 발견'}")
    return results


def format_lint_report(results: dict) -> str:
    """MCP 도구용 포맷 리포트."""
    lines = []
    total = sum(len(v) for v in results.values())
    if total == 0:
        return "✅ wiki lint 통과 — 이슈 없음"

    label_map = {
        "frontmatter": "frontmatter 필드 누락",
        "inbox": "_inbox 체류 노트",
        "empty_notes": "본문 부족 (200자 미만)",
        "isolated_nodes": "고립 노드 (링크 없음)",
        "missing_summary": "summary 없는 노드",
        "duplicate_titles": "제목 중복 노드",
    }
    for key, issues in results.items():
        if not issues:
            continue
        lines.append(f"\n### {label_map.get(key, key)} ({len(issues)}건)")
        for i in issues[:10]:  # 최대 10건만 표시
            lines.append(f"- `{i['file']}` — {i['detail']}")
        if len(issues) > 10:
            lines.append(f"  …외 {len(issues) - 10}건")

    return f"⚠️ 총 **{total}건** 이슈\n" + "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="kg_lint — Wiki 품질 점검")
    parser.add_argument("--vault", default=str(Path(get_db_root_dir())), help="vault 루트 경로")
    parser.add_argument("--fix-summary", action="store_true", help="(미구현) summary 자동 보완")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 출력")
    args = parser.parse_args()

    run_lint(Path(args.vault), fix_summary=args.fix_summary, verbose=args.verbose)


