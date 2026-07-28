"""기존 DB에 쌓인 테마/궁금증 노이즈 정리 (1회성 유지보수).

배경:
  - themes: 예전 _extract_themes가 조사를 떼지 않아 "바탕으로", "이름을",
    "연속체에서" 같은 어절이 그대로 테마로 저장됨.
  - curiosities: 반성 이벤트 감지에 워터마크/중복검사가 없어 같은 topic이
    17개까지 중복 생성됨.

사용:
  python -m scripts.dev.cleanup_themes_curiosities --dry-run
  python -m scripts.dev.cleanup_themes_curiosities --apply
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.storage.db import get_connection  # noqa: E402

# 이 스크립트는 "정규식으로 명사를 줍던 시절"의 잔재를 치우는 용도라
# 판정 규칙을 여기 안에 가지고 있다 — 본체(core/identity/service.py)는
# 이미 의미 단위 라벨만 받도록 바뀌어 이런 규칙이 없다.
_LEGACY_JOSA_TAIL = (
    "으로써", "으로서", "에게서", "으로", "에서", "에게", "부터", "까지",
    "보다", "처럼", "이나", "만큼", "에는", "에도", "을", "를", "은", "는",
    "가", "의", "로", "와", "과", "랑",
)
_LEGACY_PREDICATE_TAIL = (
    "하고", "하는", "하며", "하여", "한다", "했다", "하기", "되는", "된다",
    "됐다", "되어", "함", "됨", "임", "다", "서", "고", "며",
)
# 활동로그 상태어 — 관심사가 아니라 "무슨 일을 했나"일 뿐
_LEGACY_STATUS_WORD = {
    "완료", "실패", "성공", "추가", "삭제", "제거", "수정", "변경", "적용",
    "반영", "확인", "진행", "시작", "종료", "작성", "생성", "구현", "처리",
    "사용", "설정", "정리", "개선", "해결", "업데이트", "전체", "전면",
    "이름", "내용", "부분", "방법", "번대", "정립", "확립", "개편",
}


def _theme_is_noise(name: str) -> bool:
    """의미 단위 라벨이라면 나올 수 없는 형태인지 판정."""
    if not name or len(name) < 2:
        return True
    if name.isascii():
        return False  # engram, overlay 같은 고유명사 토큰은 유지
    if " " in name:
        return False  # "기억 연속성"처럼 띄어쓰기 있는 라벨은 새 방식 산출물
    if name in _LEGACY_STATUS_WORD:
        return True
    if name.endswith(_LEGACY_PREDICATE_TAIL):
        return True
    return any(name.endswith(j) and len(name) > len(j) + 1 for j in _LEGACY_JOSA_TAIL)


def clean_themes(conn, apply: bool) -> list[str]:
    rows = conn.execute("SELECT name FROM themes").fetchall()
    noisy = [r["name"] for r in rows if _theme_is_noise(r["name"])]
    if apply and noisy:
        conn.executemany("DELETE FROM themes WHERE name=?", [(n,) for n in noisy])
    return noisy


def dedupe_curiosities(conn, apply: bool) -> list[tuple[int, str]]:
    """topic이 같은 pending 궁금증은 가장 최근 1건만 남기고 dismiss."""
    rows = conn.execute(
        "SELECT id, topic FROM curiosities WHERE status='pending' ORDER BY id DESC"
    ).fetchall()
    seen: set[str] = set()
    dupes: list[tuple[int, str]] = []
    for r in rows:
        if r["topic"] in seen:
            dupes.append((r["id"], r["topic"]))
        else:
            seen.add(r["topic"])
    if apply and dupes:
        conn.executemany(
            "UPDATE curiosities SET status='dismissed', "
            "addressed_at=datetime('now','localtime') WHERE id=?",
            [(cid,) for cid, _ in dupes],
        )
    return dupes


def purge_legacy_themes(conn, apply: bool) -> list[str]:
    """정규식 추출 시절의 테마를 전부 제거한다.

    남아있는 맨 명사("서버", "노드")는 문법적으론 멀쩡해도 관심사 라벨이 아니다.
    decay(0.95/세션)로 자연 소멸하려면 ~44세션이 걸려 그동안 top-5를 차지하므로,
    한 번에 비우고 의미 단위 라벨로 다시 쌓게 한다.
    """
    rows = conn.execute("SELECT name FROM themes ORDER BY weight DESC").fetchall()
    names = [r["name"] for r in rows]
    if apply and names:
        conn.execute("DELETE FROM themes")
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 DB에 반영")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 결과만 출력")
    parser.add_argument(
        "--purge-legacy",
        action="store_true",
        help="정규식 시절 테마를 전부 삭제 (의미 단위 라벨로 재축적)",
    )
    parser.add_argument(
        "--purge-processed",
        action="store_true",
        help="해소/폐기된 궁금증을 전부 삭제 (대시보드 이력 뷰에서도 사라짐)",
    )
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    processed: list[tuple[int, str, str]] = []
    conn = get_connection()
    try:
        with conn:
            if args.purge_legacy:
                noisy = purge_legacy_themes(conn, apply)
            else:
                noisy = clean_themes(conn, apply)
            dupes = dedupe_curiosities(conn, apply)
            if args.purge_processed:
                rows = conn.execute(
                    "SELECT id, status, topic FROM curiosities "
                    "WHERE status IN ('addressed','dismissed') ORDER BY id"
                ).fetchall()
                processed = [(r["id"], r["status"], r["topic"]) for r in rows]
                if apply and processed:
                    conn.execute("DELETE FROM curiosities WHERE status IN ('addressed','dismissed')")
    finally:
        conn.close()

    mode = "APPLIED" if apply else "DRY-RUN"
    label = "삭제 대상 테마(legacy 전체)" if args.purge_legacy else "노이즈 테마"
    print(f"[{mode}] {label} {len(noisy)}건")
    for n in noisy:
        print(f"  - {n}")
    print(f"[{mode}] 중복 궁금증 {len(dupes)}건")
    for cid, topic in dupes:
        print(f"  - #{cid} {topic}")
    if args.purge_processed:
        print(f"[{mode}] 처리된 궁금증 삭제 {len(processed)}건")
        for cid, status, topic in processed:
            print(f"  - #{cid} [{status}] {topic[:40]}")
    if not apply:
        print("\n실제 반영하려면 --apply 로 다시 실행.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
