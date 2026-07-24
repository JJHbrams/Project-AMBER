"""trigger_type 필터링 동작 검증."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from core.context.directives import get_directives, _active_triggers

CASES = [
    ("",                           "빈 쿼리 (always만)"),
    ("wiki 노트를 작성하고 싶어",  "wiki 트리거"),
    ("버그 수정하고 코드 리팩토링", "code 트리거"),
    ("git 브랜치 만들어줘",        "git 트리거"),
    ("세션 종료할게요",             "reflection 트리거"),
    ("wiki 작성하고 커밋도 할거야", "wiki+git 복합"),
]

for query, label in CASES:
    active = _active_triggers(query)
    dirs = get_directives(user_query=query)
    keys = [f"{d['key']}({d['trigger_type']})" for d in dirs]
    print(f"\n[{label}]")
    print(f"  쿼리: {query!r}")
    print(f"  활성 트리거: {active or {'(none)'}}")
    print(f"  주입될 directives ({len(dirs)}개): {', '.join(keys)}")

