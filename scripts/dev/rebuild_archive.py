"""Claude Code transcript 원본에서 archive.db 를 다시 만든다.

왜 필요한가:
  아카이브(2026-09-12)는 STM 용으로 만든 transcript 캡처(2026-07-13) 파이프에
  얹혀 있었다. STM 에겐 흠이 아니던 것들 — 재시작마다 처음부터 재적재, 적재
  시각을 발화 시각으로 기록, SDK 경유 사용자 발화 드롭 — 이 무손실 아카이브
  에서는 전부 결함이 된다. 상류는 고쳤지만 이미 쌓인 행은 그대로라 원본에서
  다시 만든다.

원본(~/.claude/projects/**/*.jsonl)은 줄마다 uuid 와 ISO timestamp 를 갖고
있어서, 몇 번을 돌려도 같은 결과가 나온다(source_uuid UNIQUE).

사용:
  python scripts/dev/rebuild_archive.py --dry-run       # 무엇이 들어갈지만 계산
  python scripts/dev/rebuild_archive.py --purge-legacy  # uuid 없는 기존 행 제거 후 적재
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.memory.store import _normalize_source_ts  # noqa: E402
from core.memory.transcript_capture import (  # noqa: E402
    extract_turn_records,
    redact_secrets,
)
from core.storage.archive import (  # noqa: E402
    get_archive_connection,
    get_archive_path,
    initialize_archive,
)


def iter_transcripts(claude_home: Path):
    root = claude_home / "projects"
    if not root.is_dir():
        return
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for f in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            yield project_dir.name, f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="적재하지 않고 집계만 한다")
    ap.add_argument("--purge-legacy", action="store_true",
                    help="source_uuid 가 없는 기존 행을 먼저 지운다(중복·가짜 시각 행)")
    ap.add_argument("--scope-key", default="overlay")
    ap.add_argument("--claude-home", default=str(Path.home() / ".claude"))
    args = ap.parse_args()

    claude_home = Path(args.claude_home)
    print(f"transcript 원본 : {claude_home / 'projects'}")
    print(f"아카이브        : {get_archive_path()}")
    print()

    files = list(iter_transcripts(claude_home))
    if not files:
        print("transcript 를 찾지 못했다.")
        return 1

    if not args.dry_run:
        initialize_archive()

    if args.purge_legacy and not args.dry_run:
        conn = get_archive_connection()
        try:
            with conn:
                n = conn.execute("DELETE FROM turns WHERE source_uuid IS NULL").rowcount
            print(f"[purge] uuid 없는 기존 행 {n}건 삭제")
        finally:
            conn.close()

    total_records = inserted = skipped = 0
    per_project: dict[str, int] = {}

    # append_turn 은 턴마다 연결을 열고 닫는다 — 실시간 경로(턴당 1회)에는 맞지만
    # 1만 건 재적재에는 맞지 않는다. 여기서는 연결 하나로 배치 커밋한다.
    conn = None if args.dry_run else get_archive_connection()
    try:
        for project, f in files:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            records = extract_turn_records(lines)
            total_records += len(records)
            per_project[project] = per_project.get(project, 0) + len(records)

            if args.dry_run:
                continue

            rows = []
            for rec in records:
                text = redact_secrets(rec["text"])
                if not text or not text.strip():
                    continue
                rows.append((
                    None, args.scope_key, rec["role"], text,
                    rec["uuid"], _normalize_source_ts(rec["timestamp"]),
                ))
            if not rows:
                continue
            with conn:
                before = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                conn.executemany(
                    "INSERT OR IGNORE INTO turns "
                    "(session_id, scope_key, role, content, source_uuid, ts) "
                    "VALUES (?,?,?,?,?,COALESCE(?, datetime('now','localtime')))",
                    rows,
                )
                after = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            added = after - before
            inserted += added
            skipped += len(rows) - added
            print(f"  [{project[:46]:<46}] +{added:<6} (중복 {len(rows) - added})", flush=True)
    finally:
        if conn is not None:
            conn.close()

    print(f"transcript 파일 : {len(files)}개")
    print(f"추출된 턴       : {total_records}건")
    for project, n in sorted(per_project.items(), key=lambda kv: -kv[1]):
        print(f"   {n:>6}  {project}")
    if args.dry_run:
        print("\n(dry-run — 아무것도 쓰지 않았다)")
        return 0
    print(f"\n신규 적재       : {inserted}건")
    print(f"중복/빈내용 무시 : {skipped}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
