"""기존 daily note 를 프로젝트 섹션 구조로 옮긴다.

옛 구조는 시각이 바깥 축이었다.

    # 2026-09-18
    ## Engram 자동 체크포인트
    ### 12:30 — projA, projB 외 1개
    - 요약: ...

새 구조는 프로젝트가 바깥 축이고 시각이 안쪽 축이다. 한 프로젝트의 하루를
읽으려고 무관한 항목을 건너뛰지 않아도 된다.

    # projA
    ## 12:30
    - 요약: ...

제목의 프로젝트 라벨은 요약본이라("외 N개") 원본이 아니다. 항목 본문의
``- 프로젝트:`` 줄이 전부를 담고 있으므로 그쪽을 먼저 읽고, 없을 때만 제목으로
떨어진다.

기본은 dry-run 이다. ``--apply`` 를 줘야 파일을 쓴다. 그때도 ``.bak`` 를 남긴다.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config.runtime_config import get_cfg_value  # noqa: E402
from core.context.project_scope import resolve_kg_node_id  # noqa: E402

_OLD_ENTRY_RE = re.compile(
    r"^<!-- engram-checkpoint:(?P<id>[^\r\n]*?) -->\r?\n"
    r"### (?P<time>\d{1,2}:\d{2})(?: — (?P<label>[^\r\n]*))?[^\r\n]*$",
    re.MULTILINE,
)
_PROJECT_LINE_RE = re.compile(r"^- 프로젝트: (?P<value>[^\r\n]+)$", re.MULTILINE)
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FOLDED_RE = re.compile(r"\s*외 \d+개$")


def _project_name(raw: str) -> str:
    """노드 id 로 적힌 옛 라벨을 프로젝트 이름으로 되돌린다.

    옛 구조는 노드를 찾으면 라벨 자리에 **노드 id** 를 적었다. 그대로 두면 새
    기록이 만드는 ``# projectintelcontunuum`` 과 옛 기록의
    ``# project-amber-project-engram`` 이 같은 프로젝트인데도 갈라진다.
    """
    name = _LINK_RE.sub(r"\1", raw).strip()
    mapping = get_cfg_value("memory.scope.kg_node_map", {})
    if isinstance(mapping, dict):
        for key, node in mapping.items():
            if node == name:
                return key
    return re.sub(r"-overview$", "", name)


def _section_name(label: str, body: str) -> str:
    """항목이 들어갈 프로젝트 섹션 이름.

    제목 라벨은 2개까지만 적고 나머지를 접은 요약본이라, 접힌 것을 복원할 수 없다.
    본문의 프로젝트 줄이 전부를 담으므로 그쪽을 먼저 쓴다.
    """
    line = _PROJECT_LINE_RE.search(body)
    source = line.group("value") if line else _FOLDED_RE.sub("", label or "")
    return _project_name(source.split(",")[0]) or "general"


def _relink(body: str) -> str:
    """``- 프로젝트:`` 줄의 링크를 지금 규칙으로 다시 건다.

    옛 항목은 노드 id 정확 일치로 링크됐다. 그래서 대표 노트를
    ``<프로젝트>-overview.md`` 로 둔 프로젝트는 링크가 없고, 하위 보고서와 이름이
    겹친 프로젝트는 그 보고서를 가리킨다. 해소되지 않는 이름은 건드리지 않는다.
    """
    line = _PROJECT_LINE_RE.search(body)
    if not line:
        return body
    rendered = []
    for item in line.group("value").split(","):
        name = _LINK_RE.sub(r"\1", item.strip()).strip()
        if not name:
            continue
        node = resolve_kg_node_id(name)
        rendered.append(f"[[{node}]]" if node else name)
    if not rendered:
        return body
    return body[: line.start()] + f"- 프로젝트: {', '.join(rendered)}" + body[line.end():]


def _split_entries(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(머리말, [(시각, 섹션이름, 본문)]). 옛 항목이 없으면 빈 목록."""
    matches = list(_OLD_ENTRY_RE.finditer(text))
    if not matches:
        return text, []
    head = text[: matches[0].start()]
    bounds = [m.start() for m in matches] + [len(text)]
    entries = []
    for match, end in zip(matches, bounds[1:]):
        block = text[match.start(): end].strip()
        body = block.split("\n", 2)[2] if block.count("\n") >= 2 else ""
        # 섹션 이름은 재링크 **전** 본문에서 뽑는다. 재링크 뒤에는 프로젝트 이름
        # 자리에 노드 id 가 들어가 있어서 헤딩이 노드 id 가 되어버린다.
        name = _section_name(match.group("label") or "", body)
        rebuilt = f"<!-- engram-checkpoint:{match.group('id')} -->\n## {match.group('time')}\n{_relink(body)}".strip()
        entries.append((match.group("time"), name, rebuilt))
    return head, entries


def _frontmatter(head: str) -> str:
    """머리말에서 frontmatter 만 남긴다. ``# 날짜`` 와 옛 묶음 제목은 버린다."""
    if not head.startswith("---"):
        return ""
    end = head.find("\n---", 3)
    return head[: end + 4].rstrip() + "\n" if end != -1 else ""


def _minutes(value: str) -> int:
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute)


def convert(text: str) -> str | None:
    """새 구조 문서를 돌려준다. 옮길 항목이 없으면 None."""
    head, entries = _split_entries(text)
    if not entries:
        return None

    sections: dict[str, list[tuple[int, str]]] = {}
    for time_text, name, block in entries:
        sections.setdefault(name, []).append((_minutes(time_text), block))

    parts = [_frontmatter(head).rstrip()]
    for name, blocks in sections.items():
        blocks.sort(key=lambda item: item[0])
        parts.append(f"# {name}\n\n" + "\n\n".join(block for _, block in blocks))
    return "\n\n".join(part for part in parts if part).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="변환할 daily note 파일")
    parser.add_argument("--apply", action="store_true", help="실제로 쓴다(기본은 dry-run)")
    args = parser.parse_args(argv)

    changed = 0
    for path in args.paths:
        if not path.is_file():
            print(f"건너뜀 (파일 없음): {path}")
            continue
        text = path.read_text(encoding="utf-8")
        converted = convert(text)
        if converted is None:
            print(f"건너뜀 (옛 항목 없음): {path.name}")
            continue
        headings = [line for line in converted.splitlines() if line.startswith("# ")]
        print(f"{path.name}: 프로젝트 {len(headings)}개 — {', '.join(h[2:] for h in headings)}")
        if args.apply:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
            path.write_text(converted, encoding="utf-8")
        changed += 1

    print(f"\n{'적용' if args.apply else 'dry-run'}: {changed}개 파일")
    if not args.apply and changed:
        print("실제로 쓰려면 --apply 를 붙인다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
