"""기존 directives에 trigger_type 값 세팅 + DB 마이그레이션 실행."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from core.storage.db import initialize_db, get_connection

# 마이그레이션 실행 (trigger_type 컬럼 추가 포함)
initialize_db()

TRIGGER_MAP = {
    # always — 항상 주입
    "kg-first-search":          "always",
    "activity-log":             "always",
    "multi-agent-orchestration": "always",
    "read-copilot-instructions": "always",
    # wiki — wiki/문서/노트 작업 시
    "wiki-governance-trigger":  "wiki",
    "wiki-management":          "wiki",
    # code — 코딩 작업 시
    "python-import-convention": "code",
    "wiki-reminder-on-task":    "code",
    # git
    "git-branch-policy":        "git",
    # reflection — 세션종료/피드백/반성 시
    "narrative-update-guard":   "reflection",
    "reflection-trigger":       "reflection",
}

conn = get_connection()
updated = 0
with conn:
    for key, trigger in TRIGGER_MAP.items():
        cursor = conn.execute(
            "UPDATE directives SET trigger_type = ? WHERE key = ?",
            (trigger, key),
        )
        if cursor.rowcount > 0:
            updated += 1
            print(f"  {key:35s} → {trigger}")
        else:
            print(f"  {key:35s}   (not found, skipped)")
conn.close()

print(f"\n총 {updated}개 업데이트 완료.")

