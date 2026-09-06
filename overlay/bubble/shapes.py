"""Canvas에 말풍선/생각풍선/도구풍선을 그리는 순수 함수들.

모든 함수는 canvas.delete("all")로 지운 뒤 새로 그리고, 필요한 (width, height)를
반환한다 — 호출부가 그 크기로 Toplevel.geometry()를 맞춘다. 창은 캐릭터와 동일한
chroma-key(#010101) 배경 위에 그려지는 걸 전제로 한다(진짜 alpha 없음 — Windows
tkinter 제약).
"""

import logging
import math
import re
import tkinter as tk
import tkinter.font as tkfont
import webbrowser

from overlay.bubble import bubble_image, markdown_parser

log = logging.getLogger(__name__)

# 응답 말풍선 텍스트를 tk.Text 태그 대신 실제 HTML/CSS(tkinterweb)로 렌더할지 여부 —
# 임포트가 되면(빌드에 번들되면) HTML 경로를, 아니면 tk.Text 경로로 폴백한다. tkinterweb는
# 진짜 CSS(밴드 배경/둥근 테두리/박스 모델/표 보더)를 지원해 tk.Text 태그의 한계를 넘는다.
try:
    from tkinterweb import HtmlFrame as _HtmlFrame  # noqa: F401

    _HTML_AVAILABLE = True
    log.info("[bubble] tkinterweb 임포트 성공 — HTML/CSS 렌더 경로 사용")
except Exception as _e:
    _HtmlFrame = None
    _HTML_AVAILABLE = False
    log.warning("[bubble] tkinterweb 임포트 실패 — tk.Text 폴백: %r", _e)

_HTML_RENDER_FAILED_LOGGED = False  # render_rich_html 실패는 한 번만 자세히 로그
_AVAIL_LOGGED = False  # draw_speech_bubble 첫 호출 시 _HTML_AVAILABLE 상태 1회 로그

PADDING = 14
MIN_BODY_W = 80
MIN_BODY_H = 36
GRIP_SIZE = 16  # bubble_window.py의 클릭-vs-리사이즈 히트테스트와 반드시 같은 값을 써야 함
TAIL_REACH = 18  # 몸통 경계 밖으로 꼬리가 삐져나올 수 있는 최대 여유 — 사방으로 확보
                 # (크로마키로 투명이라 실제로는 안 보이는 여백이라 낭비가 아님)

# Signal Glass: restrained graphite defaults.  Theme keys remain exactly the
# same, so existing user palettes still override every surface safely.
SPEECH_BG = "#1b2029"
SPEECH_OUTLINE = "#8878d7"
SPEECH_FG = "#eef1f6"

THOUGHT_BG = "#202631"
THOUGHT_OUTLINE = "#536675"
THOUGHT_FG = "#c9d0d9"
THOUGHT_TOOL_FG = "#79bcb5"  # compact telemetry accent, not a second primary palette

INPUT_BG = "#171c25"
INPUT_OUTLINE = "#8d7cda"

TOOL_BG = "#1a2a2d"
TOOL_OUTLINE = "#5caea7"
TOOL_FG = "#c5e4df"

# Inline/codeblock colours are derived from the active shell below.  Fixed
# light pills would break graphite defaults, while fixed dark pills would make
# a user's bright theme unreadable.
CODE_FONT_FAMILY = "Consolas"

# 설정(overlay.yaml의 bubble.theme / 설정 창)에서 덮어쓸 수 있는 색상 키 전체 —
# 여기 없는 키는 무시하고, 지정 안 된 키는 위 상수(기본값)를 그대로 쓴다.
DEFAULT_THEME = {
    "speech_bg": SPEECH_BG,
    "speech_outline": SPEECH_OUTLINE,
    "speech_fg": SPEECH_FG,
    "thought_bg": THOUGHT_BG,
    "thought_outline": THOUGHT_OUTLINE,
    "thought_fg": THOUGHT_FG,
    "thought_tool_fg": THOUGHT_TOOL_FG,
    "input_bg": INPUT_BG,
    "input_outline": INPUT_OUTLINE,
    "echo_bg": "#2e251e",
    "echo_outline": "#c99a57",
    "echo_fg": "#fff1d9",
    "tool_bg": TOOL_BG,
    "tool_outline": TOOL_OUTLINE,
    "tool_fg": TOOL_FG,
    # 능동 발화(initiative nudge) — 캐릭터가 스스로 건네는 말. 답변(보라 speech)과
    # 구분되게 teal 계열 외곽선을 써서 "먼저 말 걸어온 것"임을 한눈에 알린다.
    "nudge_bg": "#1b3030",
    "nudge_outline": TOOL_OUTLINE,
    "nudge_fg": SPEECH_FG,
}

_SPINNER_FRAMES = ("●○○", "○●○", "○○●")


def draw_resize_grip(
    canvas: tk.Canvas, total_w: int, total_h: int, color: str = "#999999", corner: str = "top-right", margin: int = 0
) -> None:
    """창 상단 코너에 대각선 3줄짜리 리사이즈 손잡이를 그린다 — bubble_window.py가 같은
    코너(GRIP_SIZE x GRIP_SIZE, margin만큼 안쪽으로 들어온 지점부터)를 드래그 히트존으로 쓴다.

    하단이 아니라 상단인 이유: 이 말풍선들은 앵커(캐릭터 쪽) 근처가 아래쪽에 고정되고
    내용이 길어지면 위로 펼쳐지는 배치라, 화면 위쪽 경계에 잘리기 쉽다.

    corner: "top-right" 또는 "top-left" — 꼬리가 향하는 쪽(캐릭터 쪽)의 반대편에 둬야
    캐릭터/화면 경계에 안 가리고 자연스럽게 잡을 수 있다(호출부가 tail_side 보고 결정).

    margin: 몸통이 캔버스 가장자리로부터 안쪽으로 들어온 거리(TAIL_REACH) — 손잡이를
    캔버스 맨 끝(0)에 그리면 그 부분이 크로마키 투명 여백이라 클릭이 창을 그냥
    통과해버린다(뒷배경을 클릭한 것으로 처리됨). 반드시 실제로 그려진(불투명한) 몸통
    모서리 안쪽에 손잡이를 둬야 클릭이 이 창에 도달한다 — bubble_window.py의
    _in_grip_zone도 같은 margin을 써서 히트존을 맞춰야 한다."""
    if corner == "top-left":
        x1, y1 = margin + 3, margin + 3
        for offset in (4, 8, 12):
            canvas.create_line(x1 + offset, y1, x1, y1 + offset, fill=color, width=1)
    else:
        x1, y1 = total_w - margin - 3, margin + 3
        for offset in (4, 8, 12):
            canvas.create_line(x1 - offset, y1, x1, y1 + offset, fill=color, width=1)


def _measure_text(canvas: tk.Canvas, text: str, max_width: int, font) -> tuple[int, int]:
    tmp = canvas.create_text(0, 0, text=text, width=max_width, font=font, anchor="nw")
    bbox = canvas.bbox(tmp)
    canvas.delete(tmp)
    if bbox is None:
        return (10, 10)
    x0, y0, x1, y1 = bbox
    return (x1 - x0, y1 - y0)


def _measure_plain(text: str, font) -> int:
    return tkfont.Font(family=font[0], size=font[1]).measure(text or " ")


def _spans_plain_text(spans) -> str:
    return "".join(sp.text for sp in spans)


_SCROLLBAR_W = 10  # 스크롤바 너비(px) — 너무 넓으면 말풍선이 좁아 보임


def _scrollbar_widget(canvas: tk.Canvas, bg: str = "#444444", trough: str = "#222222") -> tk.Scrollbar:
    """캔버스 자식으로 스크롤바를 한 번만 만들어 재사용 — _rich_text_widget과 동일한 수명 관리."""
    sb = getattr(canvas, "_scrollbar_widget", None)
    if sb is not None:
        try:
            if sb.winfo_exists():
                sb.configure(bg=bg, troughcolor=trough, activebackground=bg)
                return sb
        except tk.TclError:
            pass
    sb = tk.Scrollbar(canvas, orient="vertical", width=_SCROLLBAR_W,
                      bg=bg, troughcolor=trough, activebackground=bg,
                      relief="flat", bd=0)
    canvas._scrollbar_widget = sb
    return sb


def _hide_scrollbar(canvas: tk.Canvas) -> None:
    sb = getattr(canvas, "_scrollbar_widget", None)
    if sb is not None:
        try:
            sb.place_forget()
            # 텍스트 위젯과의 연결도 끊어 마우스휠 스크롤이 남지 않게 한다
            tw = getattr(canvas, "_rich_text_widget", None)
            if tw is not None and tw.winfo_exists():
                tw.configure(yscrollcommand="")
        except Exception:
            pass


def _rich_text_widget(canvas: tk.Canvas) -> tk.Text:
    """캔버스 자식으로 텍스트 위젯을 한 번만 만들어서 재사용한다 — input_bar.py가
    Entry를 다루는 것과 같은 수명 관리 패턴이다. canvas.delete("all")은 캔버스
    아이템(도형/텍스트)만 지울 뿐 자식 위젯은 그대로 살아남으므로, 매 렌더마다
    새로 만들지 않고 내용만 갱신한다.

    Canvas.create_text로 단어 하나씩 직접 배치하던 이전 방식은 자간·정교한
    줄바꿈 제어가 Tkinter Canvas에 아예 없어서(그래서 폭 계산이 살짝만 틀려도
    글자가 테두리를 벗어나는 등) 아무리 숫자를 조정해도 한계가 있었다 — 진짜
    tk.Text 위젯을 쓰면 줄바꿈은 Tk가 정확히 처리하고, 태그로 강조/코드를
    표시하며, 마우스로 텍스트를 선택·복사하는 것도 공짜로 얻는다.

    대신 이 위젯이 놓인 영역(패딩 안쪽)에서는 마우스 클릭이 캔버스가 아니라
    이 위젯으로 먼저 간다 — 말풍선 통째로 드래그해서 옮기거나 클릭해서 닫는
    동작은 이제 텍스트 영역이 아니라 테두리(패딩) 부분에서 해야 한다(TAIL_REACH
    +PADDING 만큼 여백이 있어 잡을 자리는 충분하다). input_bar.py의 Entry도 원래
    같은 방식이었다."""
    widget = getattr(canvas, "_rich_text_widget", None)
    if widget is not None:
        try:
            if widget.winfo_exists():
                return widget
        except tk.TclError:
            pass
    widget = tk.Text(
        canvas, wrap="word", bd=0, highlightthickness=0, padx=0, pady=0,
        relief="flat", cursor="arrow", spacing2=2, takefocus=0,
    )
    widget.bind("<Key>", lambda _e: "break")  # 읽기 전용이지만 마우스 선택/복사(Ctrl+C)는 그대로 허용
    canvas._rich_text_widget = widget
    return widget


class _MdContext:
    """render_blocks가 쓰는 폰트/색/치수 묶음 — 테마색을 그대로 운반해서(하드코딩 금지)
    모든 마크다운 요소 색이 설정 창의 speech 팔레트를 따르게 한다."""

    def __init__(self, font, fg, bg, outline, code_bg, code_fg):
        family, size = font[0], font[1]
        self.family, self.size = family, size
        self.fg, self.bg, self.outline = fg, bg, outline
        self.code_bg, self.code_fg = code_bg, code_fg
        self.font_normal = (family, size)
        self.font_bold = (family, size, "bold")
        self.font_italic = (family, size, "italic")
        self.font_bolditalic = (family, size, "bold italic")
        self.font_code = (CODE_FONT_FAMILY, max(size - 1, 8))
        self.header_fonts = {
            1: (family, size + 6, "bold"),
            2: (family, size + 5, "bold"),
            3: (family, size + 3, "bold"),
            4: (family, size + 1, "bold"),
            5: (family, size, "bold"),
            6: (family, size, "bold"),
        }


# ── 응답 풍선 내부 "스타일시트" (CSS 유사 레이어) ────────────────────────────
# 마크다운 요소별 시각 규칙을 한 곳에 모은다 — 색/간격/폰트를 여기서만 바꾸면 되고,
# 나중에 설정/테마로 노출하기도 쉽다. 값 규칙:
#   font: (size_delta, weight|None, slant|None, mono)  — mono면 CODE_FONT_FAMILY, 그 외 본문 폰트
#   foreground/background: ("fg"|"bg"|"outline"|"code_fg"|"code_bg", lighten)  또는 리터럴 hex
#       lighten>0 흰색쪽, <0 검정쪽(_lighten). 0이면 원색 그대로.
#   그 외 키(spacing1/3, lmargin1/2, justify, underline, overstrike)는 tk.Text 옵션 그대로.
# 태그 이름은 markdown 렌더 로직(_render_blocks)이 참조하므로 바꾸지 말 것.
_STYLESHEET: "dict[str, dict]" = {
    "bold":       {"font": (0, "bold", None, False)},
    "italic":     {"font": (0, None, "italic", False)},
    "bolditalic": {"font": (0, "bold", "italic", False)},
    "code":       {"font": (-1, None, None, True), "foreground": ("code_fg", 0.0), "background": ("code_bg", 0.0)},
    "codeblock":  {"font": (-1, None, None, True), "foreground": ("code_fg", 0.0), "background": ("code_bg", 0.0),
                   "spacing1": 4, "spacing3": 4, "lmargin1": 8, "lmargin2": 8},
    # 헤더: h1/h2는 아웃라인(액센트) 색으로 살짝 강조해 "섹션 제목"처럼 눈에 띄게,
    # h3부터는 본문색 유지. size_delta는 ctx.header_fonts(폭 측정용)와 반드시 일치시킬 것.
    "header1":    {"font": (6, "bold", None, False), "foreground": ("outline", -0.12), "spacing1": 7, "spacing3": 6},
    "header2":    {"font": (5, "bold", None, False), "foreground": ("outline", 0.0), "spacing1": 6, "spacing3": 5},
    "header3":    {"font": (3, "bold", None, False), "foreground": ("fg", 0.0), "spacing1": 5, "spacing3": 4},
    "header4":    {"font": (1, "bold", None, False), "spacing1": 4, "spacing3": 3},
    "header5":    {"font": (0, "bold", None, False), "spacing1": 3, "spacing3": 2},
    "header6":    {"font": (0, "bold", None, False), "spacing1": 3, "spacing3": 2},
    "hr":         {"foreground": ("outline", 0.55), "justify": "center", "spacing1": 6, "spacing3": 6},
    # 인용구 — signal panel: 왼쪽 세로 강조바(▎) + 들여쓰기 + 옅은 배경 콜아웃 + 뮤트색.
    "quotebar":   {"foreground": ("outline", 0.0)},
    "quote":      {"font": (0, None, "italic", False), "foreground": ("fg", 0.3),
                   "background": ("bg", -0.05), "lmargin1": 6, "lmargin2": 22},
    "strike":     {"overstrike": True},
    "link":       {"foreground": ("outline", 0.0), "underline": True},
    "checkbox":   {"foreground": ("outline", 0.0)},
    "gap_para":   {"spacing1": 14},
    "gap_tight":  {"spacing1": 3},
    # 섹션 자동 구분선 — 하드 HR보다 옅게, 여백은 작게(divider 하나로 충분).
    "section":    {"foreground": ("outline", 0.72), "justify": "center", "spacing1": 5, "spacing3": 5},
}


def _resolve_color(spec, ctx: "_MdContext") -> str:
    if isinstance(spec, str):
        return spec  # 리터럴 hex
    key, factor = spec
    base = {"fg": ctx.fg, "bg": ctx.bg, "outline": ctx.outline, "code_fg": ctx.code_fg, "code_bg": ctx.code_bg}[key]
    return _lighten(base, factor) if factor else base


def _resolve_font(fspec, ctx: "_MdContext") -> tuple:
    size_delta, weight, slant, mono = fspec
    family = CODE_FONT_FAMILY if mono else ctx.family
    size = max(ctx.size + size_delta, 8)
    style = " ".join(x for x in (weight, slant) if x)
    return (family, size, style) if style else (family, size)


def _configure_md_tags(widget: tk.Text, ctx: "_MdContext") -> None:
    widget.config(
        font=ctx.font_normal, fg=ctx.fg, bg=ctx.bg, insertbackground=ctx.bg,
        selectbackground=_lighten(ctx.outline, 0.55), selectforeground=ctx.fg,
    )
    for tag, spec in _STYLESHEET.items():
        kwargs = {}
        for key, val in spec.items():
            if key == "font":
                kwargs["font"] = _resolve_font(val, ctx)
            elif key in ("foreground", "background"):
                kwargs[key] = _resolve_color(val, ctx)
            else:
                kwargs[key] = val
        widget.tag_configure(tag, **kwargs)


def _style_tags(styles: frozenset) -> tuple:
    """Span.styles(집합)를 tk.Text 태그 튜플로 — 폰트를 바꾸는 스타일(bold/italic/code)은
    하나만 이겨서(Tk가 나중 태그 우선) 반드시 합쳐진 단일 폰트 태그로 매핑해야 한다."""
    tags: list = []
    if "code" in styles:
        tags.append("code")
    elif "bold" in styles and "italic" in styles:
        tags.append("bolditalic")
    elif "bold" in styles:
        tags.append("bold")
    elif "italic" in styles:
        tags.append("italic")
    if "strike" in styles:
        tags.append("strike")
    if "link" in styles:
        tags.append("link")
    return tuple(tags)


def _insert_spans(widget: tk.Text, spans, extra_tags: tuple, link_ctx: dict) -> None:
    """Span 리스트를 현재 위치에 삽입. 링크는 고유 태그로 클릭 바인딩을 건다."""
    for sp in spans:
        tags = _style_tags(sp.styles) + extra_tags
        if "link" in sp.styles and sp.url:
            link_ctx["n"] += 1
            ltag = f"link_{link_ctx['n']}"
            widget.insert("end", sp.text, tags + (ltag,))
            url = sp.url
            widget.tag_bind(ltag, "<Button-1>", lambda _e, u=url: webbrowser.open(u))
            widget.tag_bind(ltag, "<Enter>", lambda _e: widget.config(cursor="hand2"))
            widget.tag_bind(ltag, "<Leave>", lambda _e: widget.config(cursor="arrow"))
        else:
            widget.insert("end", sp.text, tags)


def _list_tag(widget: tk.Text, level: int) -> str:
    """중첩 깊이별 리스트 들여쓰기 태그를 지연 생성 — 마커는 lmargin1, 접힌 줄은
    lmargin2(행잉)로 들여써서 wrap된 뒷줄이 마커 밑이 아니라 본문 밑에 정렬된다."""
    tag = f"li{level}"
    base = 6 + level * 20
    widget.tag_configure(tag, lmargin1=base, lmargin2=base + 18)
    return tag


def _render_blocks(widget: tk.Text, blocks, ctx: "_MdContext", wrap_width: int) -> int:
    """파서가 만든 블록들을 tk.Text에 렌더링하고 shrink-to-fit용 max_plain_w를 반환한다."""
    widget.config(state="normal")
    widget.delete("1.0", "end")
    # 이전 렌더의 링크 태그(이름만 쌓임)와 임베드한 표 Frame(자식 위젯)을 정리한다.
    for tag in widget.tag_names():
        if tag.startswith("link_"):
            widget.tag_delete(tag)
    for frame in getattr(widget, "_md_table_frames", []):
        try:
            frame.destroy()
        except Exception:
            pass
    widget._md_table_frames = []

    link_ctx = {"n": 0}
    max_plain_w = 0
    hr_char_w = max(_measure_plain("─", ctx.font_code), 4)
    hr_repeat = max(10, wrap_width // hr_char_w)

    def gap_tag(idx: int, prev, cur) -> tuple:
        if idx == 0:
            return ()
        both_list = isinstance(prev, markdown_parser.ListItem) and isinstance(cur, markdown_parser.ListItem)
        return ("gap_tight",) if both_list else ("gap_para",)

    P = markdown_parser
    _content_block = (P.BlockQuote, P.CodeBlock, P.Table)

    def is_section_boundary(prev, cur) -> bool:
        # 새 헤딩은 항상 새 섹션. 그 외엔 "콘텐츠 블록(인용/코드/표)" 다음에 일반 문단이
        # 이어질 때 = 인용/예시 등을 마치고 설명으로 넘어가는 지점 → 구분선.
        if prev is None:
            return False
        if isinstance(cur, P.Heading):
            return True
        if isinstance(prev, _content_block) and isinstance(cur, P.Paragraph):
            return True
        return False

    sect_repeat = max(6, hr_repeat // 2)  # 섹션 구분선은 하드 HR보다 짧게(중앙 정렬)
    prev_block = None
    for idx, block in enumerate(blocks):
        if idx > 0:
            widget.insert("end", "\n")
        if is_section_boundary(prev_block, block):
            widget.insert("end", "─" * sect_repeat + "\n", ("section",))
            gtag = ()  # divider가 이미 구분 여백을 주므로 gap_para를 겹쳐 넣지 않는다
        else:
            gtag = gap_tag(idx, prev_block, block)
        line_start = widget.index("end-1c")

        if isinstance(block, markdown_parser.Heading):
            _insert_spans(widget, block.spans, (f"header{min(block.level,6)}",) + gtag, link_ctx)
            max_plain_w = max(max_plain_w, _measure_plain(_spans_plain_text(block.spans), ctx.header_fonts[min(block.level, 6)]))

        elif isinstance(block, markdown_parser.HorizontalRule):
            widget.insert("end", "─" * hr_repeat, ("hr",) + gtag)

        elif isinstance(block, markdown_parser.CodeBlock):
            widget.insert("end", block.text, ("codeblock",) + gtag)
            for cl in block.text.split("\n"):
                max_plain_w = max(max_plain_w, _measure_plain(cl, ctx.font_code) + 12)

        elif isinstance(block, markdown_parser.BlockQuote):
            for li, line_spans in enumerate(block.lines):
                if li > 0:
                    widget.insert("end", "\n")
                q_start = widget.index("end-1c")
                widget.insert("end", "▎ ", ("quotebar",) + (gtag if li == 0 else ()))  # 왼쪽 강조바
                _insert_spans(widget, line_spans, ("quote",), link_ctx)
                widget.tag_add("quote", q_start, "end-1c")
                max_plain_w = max(max_plain_w, _measure_plain(_spans_plain_text(line_spans), ctx.font_italic) + 24)

        elif isinstance(block, markdown_parser.ListItem):
            ltag = _list_tag(widget, block.indent)
            item_start = widget.index("end-1c")
            if block.checked is not None:
                widget.insert("end", "☑  " if block.checked else "☐  ", ("checkbox",))
                marker_w = _measure_plain("☑  ", ctx.font_normal)
            elif block.ordered:
                widget.insert("end", f"{block.number}. ")
                marker_w = _measure_plain(f"{block.number}. ", ctx.font_normal)
            else:
                widget.insert("end", "•  ")
                marker_w = _measure_plain("•  ", ctx.font_normal)
            _insert_spans(widget, block.spans, (), link_ctx)
            widget.tag_add(ltag, item_start, "end-1c")
            if gtag:
                widget.tag_add(gtag[0], item_start, item_start + " lineend")
            max_plain_w = max(max_plain_w, 6 + block.indent * 20 + marker_w + _measure_plain(_spans_plain_text(block.spans), ctx.font_normal))

        elif isinstance(block, markdown_parser.Table):
            frame = _build_table_frame(widget, block, ctx, wrap_width)
            widget._md_table_frames.append(frame)
            widget.window_create("end", window=frame)
            if gtag:
                widget.tag_add(gtag[0], line_start, "end-1c")
            widget.update_idletasks()
            max_plain_w = max(max_plain_w, frame.winfo_reqwidth())

        else:  # Paragraph
            for li, line_spans in enumerate(block.lines):
                if li > 0:
                    widget.insert("end", "\n")
                _insert_spans(widget, line_spans, (gtag if li == 0 else ()), link_ctx)
                max_plain_w = max(max_plain_w, _measure_plain(_spans_plain_text(line_spans), ctx.font_normal))

        prev_block = block

    widget.config(state="disabled")
    return int(max_plain_w)


def _build_table_frame(widget: tk.Text, table, ctx: "_MdContext", wrap_width: int) -> tk.Frame:
    """표를 tk.Text 흐름에 끼워넣을 tk.Frame(grid)로 만든다. 셀 = 읽기전용 Label,
    테마색 적용, 1px 경계, 열 정렬 반영. 넓은 표는 셀 wraplength로 세로로 늘린다."""
    border = _lighten(ctx.outline, 0.35)
    ncols = len(table.aligns)
    cell_wrap = max(60, (wrap_width - 8) // max(ncols, 1) - 12)
    anchor_map = {"left": "w", "center": "center", "right": "e"}
    justify_map = {"left": "left", "center": "center", "right": "right"}

    frame = tk.Frame(widget, bg=border, bd=0, highlightthickness=1, highlightbackground=border)

    def cell(r, c, spans, header):
        align = table.aligns[c] if c < len(table.aligns) else "left"
        lbl = tk.Label(
            frame, text=_spans_plain_text(spans) or " ",
            bg=_lighten(ctx.code_bg, 0.0) if not header else ctx.code_bg,
            fg=ctx.fg, font=ctx.font_bold if header else ctx.font_normal,
            wraplength=cell_wrap, justify=justify_map[align], anchor=anchor_map[align],
            padx=8, pady=4,
        )
        lbl.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)

    for c, spans in enumerate(table.header):
        cell(0, c, spans, True)
    for r, row in enumerate(table.rows, start=1):
        for c, spans in enumerate(row):
            cell(r, c, spans, False)
    for c in range(ncols):
        frame.grid_columnconfigure(c, weight=1)
    return frame


def render_rich_text(
    canvas: tk.Canvas,
    text: str,
    max_width: int,
    font,
    fg: str,
    bg: str,
    outline: str,
    code_bg: str,
    code_fg: str,
    fixed_body_w: "int | None" = None,
) -> tuple[int, int]:
    """***bold+italic***/**bold**/*italic*/`code`, "#".."######" 헤더, "- "/"* " 불릿,
    "1. " 번호 리스트, ```코드펜스```, "---" 구분선을 실제 tk.Text 태그로 렌더링하고,
    (body_w, body_h)를 반환한다 — 위젯 자체는 canvas._rich_text_widget에 남기고
    좌표 배치(place)는 호출부(draw_speech_bubble)가 몸통 좌표를 확정한 뒤 마무리한다."""
    widget = _rich_text_widget(canvas)
    ctx = _MdContext(font, fg, bg, outline, code_bg, code_fg)
    _configure_md_tags(widget, ctx)

    wrap_width = max(fixed_body_w - PADDING * 2, 20) if fixed_body_w is not None else max_width
    blocks = markdown_parser.parse_blocks(text or " ")
    max_plain_w = _render_blocks(widget, blocks, ctx, wrap_width)

    content_w = wrap_width if fixed_body_w is not None else min(max(max_plain_w, 20), wrap_width)

    # 높이를 측정하려고 임시로 매우 크게(4000px) 배치한다 — Text.bbox()는 실제로
    # "화면에 보이는"(뷰포트 안의) 글자에 대해서만 값을 준다. height=1처럼 작게 잡은
    # 채로 측정하면 마지막 글자가 뷰포트 밖으로 밀려나 bbox가 None을 반환해서, 내용이
    # 길어져도 높이가 항상 최소값에 고정되는 버그가 된다(고정 폭으로 리사이즈한
    # 말풍선에서 텍스트가 잘려 보이는 원인). 이 임시 배치는 곧바로 draw_speech_bubble이
    # 실제 몸통 크기로 다시 place()하므로 화면에 그 크기 그대로 보이지 않는다.
    widget.place(x=TAIL_REACH + PADDING, y=TAIL_REACH + PADDING, width=content_w, height=4000)
    widget.update_idletasks()
    size = font[1]
    bbox = widget.bbox("end-1c")
    content_h = (bbox[1] + bbox[3] + 2) if bbox else int(size * 1.8)
    content_h = max(content_h, int(size * 1.8))

    body_w = fixed_body_w if fixed_body_w is not None else max(content_w + PADDING * 2, MIN_BODY_W)
    body_h = max(content_h + PADDING * 2, MIN_BODY_H)
    return body_w, body_h


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return f"#{int(max(0, min(255, r))):02x}{int(max(0, min(255, g))):02x}{int(max(0, min(255, b))):02x}"


def _lighten(hex_color: str, factor: float) -> str:
    """hex_color를 흰색 쪽으로 factor(0~1)만큼 섞는다 — 진짜 반투명(alpha)이 없는
    크로마키 캔버스에서 "바깥으로 갈수록 옅어지는 네온 글로우"를 흉내 내는 데 쓴다
    (겹겹이 그린 외곽선의 색만 점점 밝게 해서 페이드아웃처럼 보이게 함)."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex(r + (255 - r) * factor, g + (255 - g) * factor, b + (255 - b) * factor)


def _blend_hex(base: str, accent: str, amount: float) -> str:
    """Mix theme colours without introducing a separate code-palette token."""
    amount = max(0.0, min(1.0, float(amount)))
    br, bg, bb = _hex_to_rgb(base)
    ar, ag, ab = _hex_to_rgb(accent)
    return _rgb_to_hex(
        br + (ar - br) * amount,
        bg + (ag - bg) * amount,
        bb + (ab - bb) * amount,
    )


def code_tokens(bg: str, fg: str, outline: str) -> tuple[str, str]:
    """Return readable code-chip colours for either graphite or bright themes."""
    red, green, blue = _hex_to_rgb(bg)
    luminance = (red * 0.2126 + green * 0.7152 + blue * 0.0722) / 255
    # Dark glass benefits from a more visible violet/teal tint; on a bright
    # custom theme the same restrained blend stays a quiet, readable chip.
    code_bg = _blend_hex(bg, outline, 0.18 if luminance < 0.45 else 0.09)
    code_fg = _blend_hex(fg, outline, 0.22 if luminance < 0.45 else 0.30)
    return code_bg, code_fg


def _rounded_rect(canvas: tk.Canvas, x0, y0, x1, y1, radius, **kwargs):
    r = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
    points = [
        x0 + r, y0,
        x1 - r, y0,
        x1, y0,
        x1, y0 + r,
        x1, y1 - r,
        x1, y1,
        x1 - r, y1,
        x0 + r, y1,
        x0, y1,
        x0, y1 - r,
        x0, y0 + r,
        x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _tail_exit_point(body_x0, body_y0, body_w, body_h, angle_rad):
    """몸통 중심에서 angle_rad(0=오른쪽, 화면 좌표계 — 양수 y는 아래쪽) 방향으로 쏜
    직선이 몸통 사각 경계를 뚫고 나가는 점 — 여기가 꼬리가 붙는 자리다."""
    dx, dy = math.cos(angle_rad), math.sin(angle_rad)
    cx, cy = body_x0 + body_w / 2, body_y0 + body_h / 2
    half_w, half_h = body_w / 2, body_h / 2
    candidates = []
    if dx > 1e-6:
        candidates.append(half_w / dx)
    elif dx < -1e-6:
        candidates.append(-half_w / dx)
    if dy > 1e-6:
        candidates.append(half_h / dy)
    elif dy < -1e-6:
        candidates.append(-half_h / dy)
    t = min(candidates) if candidates else 0
    return cx + t * dx, cy + t * dy, dx, dy


def _draw_speech_shell(
    canvas: tk.Canvas,
    body_w: int,
    body_h: int,
    angle_rad: float,
    bg: str,
    outline: str,
    radius: int = 16,
    glow: bool = True,
) -> tuple[int, int, int, int]:
    """말풍선 몸통+꼬리만 그린다(텍스트 없음) — draw_speech_bubble과 draw_input_shell이 공유.
    radius를 작게 주면(예: 입력창) "말한 것"보다 더 각진 "입력 중" 느낌을 낼 수 있다.

    angle_rad: 몸통 중심 기준 꼬리가 향할 대상 방향(라디안) — 호출부가 화자에 맞는
    목표점을 계산해서 넘긴다(좌우 이진 방향이 아니라 대각선도 표현).
    반환값 (total_w, total_h, body_x0, body_y0) — 몸통이 캔버스 내에서 시작하는
    좌표도 같이 준다(텍스트를 그 안쪽에 배치해야 하므로).

    glow: outline 색을 밝힌 부드러운 외곽선을 하나 더 그려서 네온 아웃라인 느낌을
    낸다 — 색상은 새 설정을 추가하지 않고 기존 speech_outline/input_outline 테마
    색을 그대로 우려낸다. TAIL_REACH 여백 안에서 충분히 여유 있게 들어가므로 몸통
    크기 계산에는 영향 없음."""
    margin = TAIL_REACH
    body_x0, body_y0 = margin, margin
    body_x1, body_y1 = body_x0 + body_w, body_y0 + body_h
    total_w, total_h = body_w + margin * 2, body_h + margin * 2

    canvas.config(width=total_w, height=total_h)

    # Pillow 경로와 같은 restrained two-layer depth cue.  Every colour is
    # derived from a theme token, so a user palette never receives an unrelated
    # hard-coded accent.
    _rounded_rect(
        canvas,
        body_x0 + 2, body_y0 + 3, body_x1 + 2, body_y1 + 3,
        radius=radius, fill="", outline=_lighten(outline, -0.38), width=2,
    )

    if glow:
        # A thin edge-light replaces the former broad neon halo.
        _rounded_rect(
            canvas,
            body_x0 - 4, body_y0 - 4, body_x1 + 4, body_y1 + 4,
            radius=radius + 2, fill="", outline=_lighten(outline, 0.22), width=1,
        )

    _rounded_rect(canvas, body_x0, body_y0, body_x1, body_y1, radius=radius, fill=bg, outline=outline, width=2)
    canvas.create_line(body_x0 + radius, body_y0 + 1, body_x1 - radius, body_y0 + 1,
                       fill=_lighten(bg, 0.18), width=1)

    ex, ey, dx, dy = _tail_exit_point(body_x0, body_y0, body_w, body_h, angle_rad)
    base_half = 7
    tail_len = 16
    perp_x, perp_y = -dy, dx
    p1x, p1y = ex + perp_x * base_half, ey + perp_y * base_half
    p2x, p2y = ex - perp_x * base_half, ey - perp_y * base_half
    apex_x, apex_y = ex + dx * tail_len, ey + dy * tail_len
    canvas.create_polygon(p1x, p1y, apex_x, apex_y, p2x, p2y, fill=bg, outline=outline)
    canvas.create_oval(
        apex_x - 4, apex_y - 4, apex_x + 4, apex_y + 4,
        fill=bg, outline=_lighten(outline, 0.35), width=1,
    )
    canvas.create_oval(apex_x - 3, apex_y - 3, apex_x + 3, apex_y + 3, fill=bg, outline=outline)  # 꼬리 끝을 둥글게
    return int(total_w), int(total_h), int(body_x0), int(body_y0)


def _place_bubble_image(canvas, body_w, body_h, angle_rad, bg, outline, *, radius) -> tuple:
    """Pillow로 그린 말풍선 배경 이미지를 캔버스에 얹고 (total_w,total_h,body_x0,body_y0)
    반환 — _draw_speech_shell(Canvas 폴리곤)의 이미지 기반 대체. PhotoImage는 GC에
    민감하므로(character.py도 self._photo로 보관) canvas._bubble_photo에 참조를 남긴다.
    실패 시(PIL 문제 등) Canvas 폴리곤 방식으로 폴백해 최소한 렌더는 되게 한다."""
    try:
        photo, total_w, total_h, body_x0, body_y0 = bubble_image.build_bubble_photo(
            int(body_w), int(body_h), angle_rad, bg, outline, radius=radius, margin=TAIL_REACH
        )
        canvas.config(width=total_w, height=total_h)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas._bubble_photo = photo  # GC 방지
        return total_w, total_h, body_x0, body_y0
    except Exception:
        return _draw_speech_shell(canvas, int(body_w), int(body_h), angle_rad, bg, outline, radius=radius)


# ── HTML/CSS 렌더 경로 (tkinterweb) ─────────────────────────────────────────

_MD_STRIP_RE = re.compile(r"[*_`~#>\[\]()]|\d+\.\s|[-*]\s")


def _html_widget(canvas: tk.Canvas):
    """캔버스 자식으로 HtmlFrame을 한 번만 만들어 재사용 — _rich_text_widget과 같은
    수명 관리. canvas.delete("all")은 캔버스 아이템만 지우고 자식 위젯은 살아남는다."""
    w = getattr(canvas, "_rich_html_frame", None)
    if w is not None:
        try:
            if w.winfo_exists():
                return w
        except tk.TclError:
            pass
    # vertical_scrollbar="auto": 내용이 할당 높이를 넘칠 때만 tkinterweb 자체
    # AutoScrollbar가 나온다. 예전엔 False(=none)로 꺼놨는데, 호출부는 max_body_h로
    # 높이를 잘라내면서 HTML 경로에선 스크롤 수단을 주지 않아 "잘리기만 하고 못 읽는"
    # 상태가 됐다(tk.Text 폴백 경로만 캔버스 스크롤바를 붙였다).
    # 가로는 계속 끔 — 줄바꿈은 tkhtml이 처리하므로 가로 스크롤은 생길 일이 없다.
    w = _HtmlFrame(
        canvas, messages_enabled=False, vertical_scrollbar="auto", horizontal_scrollbar=False,
        selection_enabled=True,
    )
    try:
        w.on_link_click(lambda url: webbrowser.open(url))
    except Exception:
        pass
    canvas._rich_html_frame = w
    return w


def _sync_html_scrollbar(frame) -> None:
    """HtmlFrame 세로 스크롤바의 표시 여부를 현재 yview 기준으로 강제 재계산한다.

    tkinterweb의 AutoScrollbar는 tkhtml이 yscrollcommand를 쏠 때만 표시 여부를 다시
    판단한다. 그런데 말풍선은 "내용 로드 → 나중에 place()로 최종 높이 확정" 순서라
    높이가 줄어드는 시점에 그 콜백이 안 튀는 경우가 있다 — 그러면 내용이 넘치는데도
    스크롤바가 grid remove 된 상태로 남는다. 여기서 한 번 밀어준다.

    place() 직후에는 아직 레이아웃이 반영되지 않아 yview가 옛 값이다. after_idle 도
    tkhtml 자체 리플로우보다 먼저 도는 경우가 있어(실측), 짧은 지연을 하나 더 둔다.
    여러 번 불려도 결과는 같은 값으로 수렴하므로 중복 호출은 무해하다."""
    def _apply():
        try:
            frame._vsb.set(*frame.html.yview())
        except Exception:
            pass

    _apply()
    for delay in (0, 60):
        try:
            frame.after(delay, _apply)
        except Exception:
            pass


def _bubble_css(font, fg, bg, outline, code_bg, code_fg) -> str:
    fam, size = font[0], font[1]
    accent = _lighten(outline, -0.12)   # 헤더 강조색(아웃라인 살짝 진하게)
    muted = _lighten(fg, 0.32)          # 인용/보조 텍스트
    quote_bg = _lighten(bg, -0.05)      # 인용 콜아웃 배경
    border = _lighten(outline, 0.45)    # 표/구분 보더
    return f"""
      html,body {{ margin:0; padding:0; background:{bg}; color:{fg};
        font-family:'{fam}', sans-serif; font-size:{size}px; line-height:1.55;
        word-wrap:break-word; }}
      p {{ margin:0 0 0.6em; }}
      p:last-child {{ margin-bottom:0; }}
      h1,h2,h3,h4,h5,h6 {{ margin:0.5em 0 0.35em; line-height:1.3; }}
      h1 {{ font-size:{size + 6}px; color:{accent}; }}
      h2 {{ font-size:{size + 4}px; color:{outline}; }}
      h3 {{ font-size:{size + 2}px; }}
      h4,h5,h6 {{ font-size:{size + 1}px; }}
      b,strong {{ font-weight:bold; }}
      i,em {{ font-style:italic; }}
      code {{ font-family:'{CODE_FONT_FAMILY}',monospace; font-size:{max(size - 1, 8)}px;
        background:{code_bg}; color:{code_fg}; padding:1px 5px; border-radius:5px; }}
      pre {{ background:{code_bg}; color:{code_fg}; padding:9px 11px; border-radius:8px;
        margin:0.5em 0; }}
      pre code {{ background:transparent; padding:0; border-radius:0; }}
      blockquote {{ margin:0.5em 0; padding:6px 12px; background:{quote_bg};
        border-left:4px solid {outline}; border-radius:0 8px 8px 0; color:{muted};
        font-style:italic; }}
      ul,ol {{ margin:0.3em 0; padding-left:1.4em; }}
      li {{ margin:0.15em 0; }}
      li.task {{ list-style:none; margin-left:-1.1em; }}
      a {{ color:{outline}; text-decoration:underline; }}
      hr {{ border:none; border-top:1px solid {border}; margin:0.7em 0; }}
      table {{ border-collapse:collapse; margin:0.5em 0; }}
      th,td {{ border:1px solid {border}; padding:3px 9px; }}
      th {{ background:{quote_bg}; }}
    """


def _estimate_md_width(text: str, font) -> int:
    """마크다운 텍스트의 대략적인 자연 콘텐츠 폭(px) — HtmlFrame은 bbox로 폭을 안
    알려주므로(항상 프레임폭 반환) 여기서 tkfont로 가장 긴 줄을 재서 추정한다. 표시
    폭이 살짝 어긋나도 줄바꿈은 tkhtml이 알아서 하므로 안전한 근사면 충분하다."""
    f = tkfont.Font(family=font[0], size=font[1])
    widest = 0
    for raw in (text or " ").split("\n"):
        plain = _MD_STRIP_RE.sub("", raw).strip()
        if plain:
            widest = max(widest, f.measure(plain))
    return widest + 16  # bold/헤더/마커가 조금 더 넓으므로 여유


def render_rich_html(canvas, text, max_width, font, fg, bg, outline, code_bg, code_fg, fixed_body_w=None):
    """마크다운을 HTML/CSS로 렌더(tkinterweb)하고 (body_w, body_h)를 반환 — 반환 계약은
    render_rich_text와 동일(호출부가 body+PADDING 안쪽에 위젯을 place). 실패 시 None을
    반환해 호출부가 tk.Text 경로로 폴백하게 한다.

    폭: HtmlFrame이 자연폭을 안 주므로 _estimate_md_width로 추정(고정폭이면 그대로).
    높이: frame.html.bbox()[3]가 실제 콘텐츠 높이를 정확히 준다."""
    try:
        frame = _html_widget(canvas)
        wrap_width = (fixed_body_w - PADDING * 2) if fixed_body_w is not None else max_width
        if fixed_body_w is not None:
            content_w = wrap_width
        else:
            content_w = min(max(_estimate_md_width(text, font), 20), wrap_width)
        css = _bubble_css(font, fg, bg, outline, code_bg, code_fg)
        body_html = markdown_parser.to_html(text or " ")
        html = f"<html><head><style>{css}</style></head><body>{body_html}</body></html>"
        # 프레임 배경색은 CSS의 html,body{background}로 채운다 — HtmlFrame은 tk의 -bg
        # 옵션을 안 받으므로 frame.configure(bg=...)를 호출하면 TclError로 렌더 전체가
        # 폴백돼 버린다(과거 버그). 굳이 컨테이너 bg가 필요하면 지원되는 옵션으로만.
        frame.place(x=TAIL_REACH + PADDING, y=TAIL_REACH + PADDING, width=content_w, height=4000)
        frame.load_html(html)
        frame.update_idletasks()
        try:
            bb = frame.html.bbox()
            content_h = bb[3] if bb else int(font[1] * 3)
        except Exception:
            content_h = int(font[1] * 3)
        content_h = max(content_h, int(font[1] * 1.8))
        body_w = fixed_body_w if fixed_body_w is not None else content_w + PADDING * 2
        body_h = content_h + PADDING * 2
        return body_w, body_h
    except Exception:
        global _HTML_RENDER_FAILED_LOGGED
        if not _HTML_RENDER_FAILED_LOGGED:
            _HTML_RENDER_FAILED_LOGGED = True
            log.exception("[bubble] HTML 렌더 실패 — tk.Text 폴백")
        return None


def draw_speech_bubble(
    canvas: tk.Canvas,
    text: str,
    max_width: int,
    angle_rad: float = 0.0,
    font=("Noto Sans KR Medium", 11),
    fg: str = SPEECH_FG,
    bg: str = SPEECH_BG,
    outline: str = SPEECH_OUTLINE,
    fixed_body_w: "int | None" = None,
    grip_corner: str = "top-right",
    max_body_h: "int | None" = None,
) -> tuple[int, int]:
    """fixed_body_w: 사용자가 grip으로 폭을 직접 정한 경우 — 짧은 텍스트라도 그 폭으로
    고정한다(기본은 텍스트에 맞춰 줄어드는 shrink-to-fit).
    angle_rad: 꼬리가 향할 방향(캐릭터 쪽) — 호출부가 실시간 위치 기준으로 계산해서 넘김.
    grip_corner: 리사이즈 손잡이 위치 — 호출부가 캐릭터 반대쪽으로 넘겨야 한다.

    텍스트는 tkinterweb(HTML/CSS)가 있으면 그걸로, 없으면 tk.Text 태그로 렌더한다.
    HTML 경로는 밴드 배경/둥근 콜아웃/표 보더 등 진짜 CSS 스타일을 준다. 둘 다
    Pillow 말풍선 이미지 위에 위젯을 얹는 방식은 동일하다."""
    canvas.delete("all")
    global _AVAIL_LOGGED
    if not _AVAIL_LOGGED:
        _AVAIL_LOGGED = True
        log.info("[bubble] draw_speech_bubble 첫 호출 — _HTML_AVAILABLE=%s", _HTML_AVAILABLE)
    used_html = False
    code_bg, code_fg = code_tokens(bg, fg, outline)
    if _HTML_AVAILABLE:
        sized = render_rich_html(canvas, text or " ", max_width, font, fg, bg, outline, code_bg, code_fg, fixed_body_w)
        if sized is not None:
            body_w, body_h = sized
            used_html = True
    if not used_html:
        body_w, body_h = render_rich_text(canvas, text or " ", max_width, font, fg, bg, outline, code_bg, code_fg, fixed_body_w)

    needs_scroll = (not used_html) and (max_body_h is not None) and (body_h > max_body_h)
    if max_body_h is not None:
        body_h = min(body_h, max_body_h)

    total_w, total_h, body_x0, body_y0 = _place_bubble_image(canvas, body_w, body_h, angle_rad, bg, outline, radius=16)
    widget = _html_widget(canvas) if used_html else _rich_text_widget(canvas)

    inner_x = body_x0 + PADDING
    inner_y = body_y0 + PADDING
    inner_w = body_w - PADDING * 2
    inner_h = body_h - PADDING * 2

    if needs_scroll:
        sb = _scrollbar_widget(canvas, bg=outline, trough=bg)
        text_w = inner_w - _SCROLLBAR_W - 2
        widget.configure(yscrollcommand=sb.set)
        sb.configure(command=widget.yview)
        widget.place(x=inner_x, y=inner_y, width=text_w, height=inner_h)
        sb.place(x=inner_x + text_w + 2, y=inner_y, width=_SCROLLBAR_W, height=inner_h)
        sb.lift()
    else:
        _hide_scrollbar(canvas)
        widget.place(x=inner_x, y=inner_y, width=inner_w, height=inner_h)

    if used_html:
        # HTML 경로는 캔버스 스크롤바(_scrollbar_widget) 대신 HtmlFrame 자체 스크롤바를
        # 쓴다. 높이는 위에서 max_body_h로 잘렸으므로, 넘치면 여기서 스크롤바가 나온다.
        _sync_html_scrollbar(widget)

    widget.lift()
    draw_resize_grip(canvas, total_w, total_h, color=outline, corner=grip_corner, margin=TAIL_REACH)
    return total_w, total_h


def draw_input_shell(
    canvas: tk.Canvas,
    body_w: int,
    body_h: int,
    angle_rad: float = 0.0,
    bg: str = INPUT_BG,
    outline: str = INPUT_OUTLINE,
    grip_corner: str = "top-right",
) -> tuple[int, int]:
    """입력창(InputBar)용 — 실제 내용은 그 위에 얹는 tk.Entry가 그리므로, 여기선 원하는
    body_w/body_h 그대로 말풍선 껍데기만 그린다(텍스트 측정으로 크기가 줄어들지 않음).
    말풍선(oval 느낌)보다 각진 radius를 써서 "이미 말한 것"과 "입력 중"을 구분한다."""
    canvas.delete("all")
    total_w, total_h, _body_x0, _body_y0 = _place_bubble_image(
        canvas, max(body_w, MIN_BODY_W), max(body_h, MIN_BODY_H), angle_rad, bg, outline, radius=10
    )
    # The compact send beacon is deliberately Canvas-native: it supplies a
    # strong visual endpoint while Enter remains the accessible send action.
    cx, cy = total_w - TAIL_REACH - 17, total_h / 2
    canvas.create_oval(cx - 11, cy - 11, cx + 11, cy + 11,
                       fill=_lighten(outline, -0.20), outline=outline, width=1)
    canvas.create_line(cx - 3, cy - 4, cx + 5, cy, cx - 3, cy + 4,
                       fill=_lighten(bg, 0.72), width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)
    draw_resize_grip(canvas, total_w, total_h, color=outline, corner=grip_corner, margin=TAIL_REACH)
    return total_w, total_h


def draw_thought_bubble(
    canvas: tk.Canvas,
    text: str,
    max_width: int,
    angle_rad: float = -math.pi / 2,
    font=("Noto Sans KR Medium", 11),
    fg: str = THOUGHT_FG,
    bg: str = THOUGHT_BG,
    outline: str = THOUGHT_OUTLINE,
    fixed_body_w: "int | None" = None,
    tool_lines: str = "",
    tool_fg: str = THOUGHT_TOOL_FG,
    grip_corner: str = "top-right",
    max_body_h: "int | None" = None,
) -> tuple[int, int]:
    """angle_rad: 몸통 중심 기준 캐릭터가 있는 방향(라디안, 기본값은 위쪽 -90도 —
    캐릭터 머리 바로 위 중앙 배치의 기존 기본 케이스). draw_speech_bubble과 동일하게
    _tail_exit_point로 경계 교차점을 구하고, 거기서부터 원형 체인(생각풍선 특유의
    동글동글한 꼬리)을 그 방향으로 이어간다 — 삼각형 꼬리(대화풍선)와 구분되는 모양.
    fixed_body_w: draw_speech_bubble과 동일 — grip으로 직접 정한 폭을 고정.
    tool_lines: 도구 상태 줄들(있으면 text 아래에 tool_fg 색으로 따로 그림 — 생각과
    구분되게)."""
    canvas.delete("all")
    if text and tool_lines:
        combined = f"{text}\n\n{tool_lines}"
    else:
        combined = text or tool_lines
    text_w, text_h = _measure_text(canvas, combined or " ", max_width, font)
    body_w = fixed_body_w if fixed_body_w is not None else max(text_w + PADDING * 2, MIN_BODY_W + 10)
    body_h = max(text_h + PADDING * 2, MIN_BODY_H + 10)
    needs_scroll = (max_body_h is not None) and (body_h > max_body_h)
    if max_body_h is not None:
        body_h = min(body_h, max_body_h)

    margin = TAIL_REACH
    body_x0, body_y0 = margin, margin
    body_x1, body_y1 = body_x0 + body_w, body_y0 + body_h
    total_w, total_h = body_w + margin * 2, body_h + margin * 2

    canvas.config(width=total_w, height=total_h)

    # Thought/tool status is deliberately a low-contrast telemetry ribbon,
    # distinct from the premium response panel without changing its placement
    # or tail geometry contract.
    _rounded_rect(canvas, body_x0 + 1, body_y0 + 2, body_x1 + 1, body_y1 + 2,
                  radius=12, fill="", outline=_lighten(outline, -0.35), width=2)
    _rounded_rect(canvas, body_x0, body_y0, body_x1, body_y1,
                  radius=12, fill=bg, outline=outline, width=1)
    canvas.create_line(body_x0 + 14, body_y0 + 2, body_x1 - 14, body_y0 + 2,
                       fill=_lighten(bg, 0.16), width=1)
    canvas.create_line(body_x0 + 10, body_y0 + 11, body_x0 + 10, body_y1 - 11,
                       fill=tool_fg, width=2)

    ex, ey, dx, dy = _tail_exit_point(body_x0, body_y0, body_w, body_h, angle_rad)
    cx, cy = ex, ey
    for sz in (10, 6, 4):
        canvas.create_oval(cx - sz, cy - sz, cx + sz, cy + sz, fill=bg, outline=outline)
        cx += dx * (sz + 4)
        cy += dy * (sz * 0.4 + 4)

    inner_x = body_x0 + PADDING
    inner_y = body_y0 + PADDING
    inner_w = body_w - PADDING * 2
    inner_h = body_h - PADDING * 2

    widget = _rich_text_widget(canvas)
    widget.configure(state="normal", font=font, bg=bg, fg=fg,
                     bd=0, highlightthickness=0, relief="flat", cursor="arrow", wrap="word")
    widget.tag_configure("tool", foreground=tool_fg)
    widget.delete("1.0", "end")
    if text:
        widget.insert("end", text)
    if text and tool_lines:
        widget.insert("end", "\n\n")
    if tool_lines:
        widget.insert("end", tool_lines, "tool")
    widget.configure(state="disabled")

    if needs_scroll:
        sb = _scrollbar_widget(canvas, bg=outline, trough=bg)
        tw = inner_w - _SCROLLBAR_W - 2
        widget.configure(yscrollcommand=sb.set)
        sb.configure(command=widget.yview)
        widget.place(x=inner_x, y=inner_y, width=tw, height=inner_h)
        sb.place(x=inner_x + tw + 2, y=inner_y, width=_SCROLLBAR_W, height=inner_h)
        sb.lift()
    else:
        _hide_scrollbar(canvas)
        widget.place(x=inner_x, y=inner_y, width=inner_w, height=inner_h)
    widget.lift()

    draw_resize_grip(canvas, int(total_w), int(total_h), color=outline, corner=grip_corner, margin=TAIL_REACH)
    return int(total_w), int(total_h)


def draw_tool_bubble(
    canvas: tk.Canvas,
    tool_name: str,
    status: str,
    max_width: int,
    anim_frame: int = 0,
    font=("Noto Sans KR Medium", 10),
    fg: str = TOOL_FG,
    bg: str = TOOL_BG,
    outline: str = TOOL_OUTLINE,
) -> tuple[int, int]:
    """status: 'running' | 'ok' | 'error' | 'ask'(승인 대기)."""
    canvas.delete("all")
    if status == "ok":
        indicator = "✓"
    elif status == "error":
        indicator = "✗"
    elif status == "ask":
        indicator = "❓"
    else:
        indicator = _SPINNER_FRAMES[anim_frame % len(_SPINNER_FRAMES)]
    label = f"{indicator}  {tool_name}"

    text_w, text_h = _measure_text(canvas, label, max_width, font)
    w = max(text_w + PADDING * 2, 80)
    h = max(text_h + PADDING * 2, 26)

    canvas.config(width=w, height=h)
    _rounded_rect(canvas, 0, 0, w, h, radius=8, fill=bg, outline=outline)
    canvas.create_text(PADDING, h / 2, text=label, font=font, fill=fg, anchor="w")
    return int(w), int(h)
