"""응답 말풍선 텍스트를 위한 순수 마크다운 파서(Tkinter 비의존 — 헤드리스 테스트 가능).

이전에는 shapes.py 안에서 정규식 특수 케이스를 계속 덧대는 방식이라 새 마크다운 요소가
나올 때마다 원문 그대로 노출되는 문제가 반복됐다. 여기서는 CommonMark의 부분집합을
"블록 단위로 먼저 나누고(parse_blocks) 각 블록 안의 인라인을 스캔(parse_inline)"하는
2단 구조로 재작성한다 — 새 요소 추가가 정규식 교체가 아니라 케이스 하나 추가로 끝난다.

렌더링(tk.Text 태그/window_create)은 이 모듈이 반환하는 데이터 모델을 shapes.py 쪽
render_blocks가 소비해서 처리한다. 이 모듈은 데이터만 만든다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 인라인 데이터 모델 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Span:
    """인라인 텍스트 조각 하나. styles는 {"bold","italic","code","strike","link"}의
    부분집합(굵게+기울임은 {"bold","italic"}). link일 때만 url이 채워진다."""

    text: str
    styles: frozenset = field(default_factory=frozenset)
    url: "str | None" = None


# ── 블록 데이터 모델 ────────────────────────────────────────────────────────


@dataclass
class Heading:
    level: int  # 1~6
    spans: list


@dataclass
class Paragraph:
    # 각 원소가 한 물리적 줄의 spans — 빈 줄 없이 이어진 줄들을 한 문단으로 묶는다.
    # (시처럼 줄바꿈이 의미 있는 경우를 위해 줄 구분을 보존한다.)
    lines: list


@dataclass
class ListItem:
    ordered: bool
    indent: int  # 중첩 깊이(0=최상위)
    number: "int | None"  # 번호 리스트면 그 번호
    checked: "bool | None"  # 체크박스면 True/False, 아니면 None
    spans: list


@dataclass
class CodeBlock:
    text: str
    lang: str = ""


@dataclass
class BlockQuote:
    depth: int
    lines: list  # 각 원소가 한 줄의 spans


@dataclass
class HorizontalRule:
    pass


@dataclass
class Table:
    aligns: list  # 열별 "left"/"center"/"right"
    header: list  # list[list[Span]] — 헤더 셀들
    rows: list  # list[list[list[Span]]] — 행 × 셀 × spans


# ── 인라인 파서 (구분자 스캐너) ─────────────────────────────────────────────

_ASTERISK_MARKERS = [("***", frozenset({"bold", "italic"})), ("**", frozenset({"bold"})), ("*", frozenset({"italic"}))]
_UNDERSCORE_MARKERS = [("___", frozenset({"bold", "italic"})), ("__", frozenset({"bold"})), ("_", frozenset({"italic"}))]
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_WORD_RE = re.compile(r"\w")


def _is_wordish(ch: "str | None") -> bool:
    return bool(ch) and bool(_WORD_RE.match(ch))


def parse_inline(text: str) -> list:
    """text를 Span 리스트로 변환. 스타일이 누적되도록 재귀적으로 파싱한다
    (예: 링크 안의 **굵게** → styles={"bold","link"})."""
    return _parse_inline(text, frozenset(), None)


def _parse_inline(text: str, styles: frozenset, url: "str | None") -> list:
    spans: list = []
    buf: list = []
    i, n = 0, len(text)

    def flush():
        if buf:
            spans.append(Span("".join(buf), styles, url))
            buf.clear()

    while i < n:
        matched = _try_delim(text, i, styles, url)
        if matched is not None:
            inner, consumed = matched
            flush()
            spans.extend(inner)
            i += consumed
        else:
            buf.append(text[i])
            i += 1
    flush()
    return spans


def _try_delim(text: str, i: int, styles: frozenset, url: "str | None"):
    """위치 i에서 시작하는 인라인 마크업을 시도 → (inner_spans, consumed) 또는 None.
    긴 마커부터(*** > ** > *) 시도해야 겹침 오매칭을 피한다."""
    ch = text[i]

    # 코드 스팬 — 백틱 런(k개) 매칭, 내부는 리터럴(추가 파싱 안 함).
    if ch == "`":
        k = 0
        while i + k < len(text) and text[i + k] == "`":
            k += 1
        close = text.find("`" * k, i + k)
        # 닫는 런이 정확히 k개여야 함(더 길면 그 앞까지)
        while close != -1:
            after = close + k
            if after >= len(text) or text[after] != "`":
                content = text[i + k : close]
                if content:
                    return [Span(content, styles | {"code"}, url)], after - i
                break
            close = text.find("`" * k, after)
        return None

    # 취소선
    if text.startswith("~~", i):
        close = text.find("~~", i + 2)
        if close != -1 and close > i + 2:
            inner = _parse_inline(text[i + 2 : close], styles | {"strike"}, url)
            return inner, (close + 2) - i
        return None

    # 링크 [text](url)
    if ch == "[":
        m = _LINK_RE.match(text, i)
        if m:
            inner = _parse_inline(m.group(1), styles | {"link"}, m.group(2))
            return inner, m.end() - i
        return None

    # 별표 강조 (*, **, ***)
    if ch == "*":
        for marker, add in _ASTERISK_MARKERS:
            if text.startswith(marker, i):
                close = _find_emphasis_close(text, i + len(marker), marker)
                if close != -1:
                    inner = _parse_inline(text[i + len(marker) : close], styles | add, url)
                    return inner, (close + len(marker)) - i
        return None

    # 밑줄 강조 (_, __, ___) — intra-word 가드: 앞뒤가 단어문자면 리터럴(snake_case 보호)
    if ch == "_":
        prev = text[i - 1] if i > 0 else None
        for marker, add in _UNDERSCORE_MARKERS:
            if text.startswith(marker, i):
                if _is_wordish(prev):
                    return None  # foo_bar — 강조 아님
                close = _find_emphasis_close(text, i + len(marker), marker)
                if close != -1:
                    nxt = text[close + len(marker)] if close + len(marker) < len(text) else None
                    if _is_wordish(nxt):
                        return None
                    inner = _parse_inline(text[i + len(marker) : close], styles | add, url)
                    return inner, (close + len(marker)) - i
        return None

    return None


def _find_emphasis_close(text: str, start: int, marker: str) -> int:
    """start부터 marker의 닫는 위치를 찾는다(비어있지 않은 내용 보장). 없으면 -1."""
    j = text.find(marker, start)
    while j != -1:
        if j > start:  # 내용이 비어있지 않음
            return j
        j = text.find(marker, j + 1)
    return -1


# ── 블록 파서 (줄 상태머신) ─────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_LIST_RE = re.compile(r"^([-*+]|\d{1,9}[.)])\s+(.*)$")
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_INDENT_UNIT = 2  # 들여쓰기 이 칸수마다 중첩 한 단계


def parse_blocks(text: str) -> list:
    lines = (text or "").split("\n")
    blocks: list = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # 1) 코드펜스 — 내부는 인라인 파싱 안 함
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf: list = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 닫는 펜스(없이 끝나도 안전)
            blocks.append(CodeBlock("\n".join(buf), lang))
            continue

        # 2) 빈 줄 — 문단 경계
        if stripped == "":
            i += 1
            continue

        # 3) 테이블 — 현재 줄이 표 행이고 다음 줄이 구분행
        if _is_table_row(raw) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            header = [parse_inline(c) for c in _split_row(raw)]
            aligns = _parse_aligns(lines[i + 1])
            i += 2
            rows: list = []
            while i < n and lines[i].strip() != "" and _is_table_row(lines[i]):
                rows.append([parse_inline(c) for c in _split_row(lines[i])])
                i += 1
            blocks.append(_normalize_table(header, aligns, rows))
            continue

        # 4) 수평선
        if _HR_RE.match(stripped):
            blocks.append(HorizontalRule())
            i += 1
            continue

        # 5) 헤딩
        hm = _HEADING_RE.match(stripped)
        if hm:
            blocks.append(Heading(len(hm.group(1)), parse_inline(hm.group(2))))
            i += 1
            continue

        # 6) 인용 — 연속 인용줄을 한 블록으로
        if stripped.startswith(">"):
            qlines: list = []
            depth = 1
            while i < n and lines[i].strip().startswith(">"):
                content, d = _strip_quote(lines[i])
                depth = max(depth, d)
                qlines.append(parse_inline(content))
                i += 1
            blocks.append(BlockQuote(depth, qlines))
            continue

        # 7) 리스트 아이템 (들여쓰기로 중첩 판정)
        expanded = raw.expandtabs(4)
        indent_spaces = len(expanded) - len(expanded.lstrip(" "))
        lm = _LIST_RE.match(expanded.lstrip(" "))
        if lm:
            marker, item_body = lm.group(1), lm.group(2)
            ordered = marker[0].isdigit()
            number = int(marker[:-1]) if ordered else None
            checked: "bool | None" = None
            cb = _CHECKBOX_RE.match(item_body)
            if cb:
                checked = cb.group(1) in ("x", "X")
                item_body = cb.group(2)
            blocks.append(
                ListItem(
                    ordered=ordered,
                    indent=indent_spaces // _INDENT_UNIT,
                    number=number,
                    checked=checked,
                    spans=parse_inline(item_body),
                )
            )
            i += 1
            continue

        # 8) 문단 — 빈 줄/특수 블록 전까지의 연속 줄을 하나로
        para_lines: list = [parse_inline(raw.strip())]
        i += 1
        while i < n:
            look = lines[i]
            ls = look.strip()
            if ls == "" or ls.startswith("```") or ls.startswith(">") or _HEADING_RE.match(ls) or _HR_RE.match(ls):
                break
            if _LIST_RE.match(look.expandtabs(4).lstrip(" ")):
                break
            if _is_table_row(look) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
                break
            para_lines.append(parse_inline(ls))
            i += 1
        blocks.append(Paragraph(para_lines))

    return blocks


# ── 표 헬퍼 ─────────────────────────────────────────────────────────────────


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return "|" in s and s not in ("|", "||")


def _split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_aligns(sep_line: str) -> list:
    aligns: list = []
    for cell in _split_row(sep_line):
        c = cell.strip()
        left = c.startswith(":")
        right = c.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns


def _normalize_table(header: list, aligns: list, rows: list) -> Table:
    ncols = max([len(header)] + [len(r) for r in rows] + [len(aligns)])
    empty = [Span("")]

    def pad(cells):
        return cells + [[Span("")] for _ in range(ncols - len(cells))]

    header = pad(header)
    rows = [pad(r) for r in rows]
    aligns = (aligns + ["left"] * ncols)[:ncols]
    return Table(aligns=aligns, header=header, rows=rows)


# ── 인용 헬퍼 ───────────────────────────────────────────────────────────────


def _strip_quote(line: str) -> tuple:
    """'> ' / '>> ' 접두어를 벗기고 (내용, depth) 반환."""
    s = line.strip()
    depth = 0
    while s.startswith(">"):
        depth += 1
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s, depth


# ── HTML 변환 (tkinterweb 렌더 경로용) ──────────────────────────────────────
# parse_blocks 결과를 HTML 문자열로 직렬화한다. tk.Text 태그 렌더러와 별개로,
# HtmlFrame(tkinterweb)이 진짜 HTML/CSS로 렌더할 수 있게 한다(밴드 배경/둥근 테두리/
# 박스 모델 등 tk.Text로는 불가능한 스타일). 스타일은 여기서 넣지 않고(클래스만 부여)
# CSS는 shapes.py가 테마에서 생성해 주입한다 — 의미(semantic)와 스타일 분리.

_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _esc(text: str) -> str:
    return "".join(_HTML_ESCAPES.get(c, c) for c in text)


def _span_to_html(sp: "Span") -> str:
    inner = _esc(sp.text)
    st = sp.styles
    if "code" in st:
        return f"<code>{inner}</code>"  # 코드 안에서는 다른 강조 중첩 안 함
    if "link" in st and sp.url:
        inner = f'<a href="{_esc(sp.url)}">{inner}</a>'
    if "bold" in st:
        inner = f"<b>{inner}</b>"
    if "italic" in st:
        inner = f"<i>{inner}</i>"
    if "strike" in st:
        inner = f"<s>{inner}</s>"
    return inner


def _spans_to_html(spans: list) -> str:
    return "".join(_span_to_html(sp) for sp in spans)


def _lines_to_html(lines: list) -> str:
    # lines: list[list[Span]] — 줄바꿈은 <br>로.
    return "<br>".join(_spans_to_html(line) for line in lines)


def _list_run_to_html(items: list) -> str:
    """연속된 ListItem들을 indent 기준 중첩 <ul>/<ol>로 직렬화한다."""
    out: list = []
    stack: list = []  # [indent, tag] 열려 있는 리스트들. 각 레벨엔 "열린 <li>" 하나가 딸림
    #                   (자식 리스트가 그 <li> 안에 중첩되도록 li를 미리 안 닫는다).

    def li_html(it) -> str:
        if it.checked is not None:
            return f'<li class="task">{"☑" if it.checked else "☐"} {_spans_to_html(it.spans)}'
        return f"<li>{_spans_to_html(it.spans)}"  # </li>는 나중에(중첩 대비) 닫는다

    for it in items:
        tag = "ol" if it.ordered else "ul"
        if not stack:
            out.append(f"<{tag}>")
            stack.append([it.indent, tag])
        elif it.indent > stack[-1][0]:
            # 더 깊이 — 직전 <li>는 열어둔 채 그 안에 자식 리스트를 연다.
            out.append(f"<{tag}>")
            stack.append([it.indent, tag])
        else:
            # 같거나 얕은 레벨 — 더 깊은 레벨들을 닫는다(각 레벨의 열린 li + 그 리스트).
            # 부모 li는 여기서 닫지 않는다 — 루프 후의 같은레벨 </li>가 담당한다(중복 방지).
            while len(stack) > 1 and it.indent < stack[-1][0]:
                out.append(f"</li></{stack[-1][1]}>")
                stack.pop()
            out.append("</li>")  # 같은 레벨의 직전 형제(또는 자식 리스트를 담던 부모) <li> 닫기
            if stack[-1][1] != tag:  # 같은 레벨에서 ol↔ul 전환 → 리스트 교체
                out.append(f"</{stack[-1][1]}><{tag}>")
                stack[-1][1] = tag
        out.append(li_html(it))
    while stack:
        out.append(f"</li></{stack[-1][1]}>")
        stack.pop()
    return "".join(out)


def _table_to_html(tbl: "Table") -> str:
    out = ["<table>"]
    if tbl.header:
        out.append("<thead><tr>")
        for i, cell in enumerate(tbl.header):
            align = tbl.aligns[i] if i < len(tbl.aligns) else "left"
            out.append(f'<th style="text-align:{align}">{_spans_to_html(cell)}</th>')
        out.append("</tr></thead>")
    out.append("<tbody>")
    for row in tbl.rows:
        out.append("<tr>")
        for i, cell in enumerate(row):
            align = tbl.aligns[i] if i < len(tbl.aligns) else "left"
            out.append(f'<td style="text-align:{align}">{_spans_to_html(cell)}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def blocks_to_html(blocks: list) -> str:
    """parse_blocks 결과 → HTML body 내부 문자열(스타일 없음, 클래스만)."""
    out: list = []
    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        if isinstance(b, ListItem):
            run = []
            while i < n and isinstance(blocks[i], ListItem):
                run.append(blocks[i])
                i += 1
            out.append(_list_run_to_html(run))
            continue
        if isinstance(b, Heading):
            lvl = min(max(b.level, 1), 6)
            out.append(f"<h{lvl}>{_spans_to_html(b.spans)}</h{lvl}>")
        elif isinstance(b, Paragraph):
            out.append(f"<p>{_lines_to_html(b.lines)}</p>")
        elif isinstance(b, BlockQuote):
            out.append(f"<blockquote>{_lines_to_html(b.lines)}</blockquote>")
        elif isinstance(b, CodeBlock):
            out.append(f"<pre><code>{_esc(b.text)}</code></pre>")
        elif isinstance(b, HorizontalRule):
            out.append("<hr>")
        elif isinstance(b, Table):
            out.append(_table_to_html(b))
        i += 1
    return "\n".join(out)


def to_html(text: str) -> str:
    """마크다운 텍스트 → HTML body 내부 문자열."""
    return blocks_to_html(parse_blocks(text or ""))
