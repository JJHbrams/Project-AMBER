"""여러 풍선(대화/생각/도구)을 조율한다 — session.py의 BubbleEvent(dict)를 받아 렌더링.

슬롯 정책:
- 대화(speech): 캐릭터 반대쪽 위(오버레이가 우측하단이면 좌측 상단)에 뜨는 독립 슬롯.
  사용자가 보낸 말과 어시스턴트의 답이 같은 슬롯을 쓰지만, 새 턴이 시작되면 즉시
  교체된다 — "다음 턴이 오거나 사용자가 클릭할 때까지" 화면에 유지된다(자동 타이머로
  사라지지 않음).
- 생각(thought) + 도구(tool_use/tool_result): 대화 풍선과 완전히 독립적으로 캐릭터
  머리 바로 위에 뜨는 슬롯. 도구 사용 안내는 별도 풍선 스택이 아니라 이 생각 풍선
  안에 상태 줄로 얹는다(예: "⏳ Read", 완료되면 "✓ Read"). "지금 무슨 생각/작업
  중인지"가 목적이라 turn_end(응답 생성 완료)가 오면 바로 치운다 — 개별 도구 줄의
  tool_dwell_ms 만료를 기다릴 필요도 없이 한 번에 정리된다.

on_event(ev) 하나만 호출하면 되는 게 이 클래스의 유일한 공개 계약 — session.py는
tkinter를 몰라도 되고, 이 클래스는 SDK 이벤트 스키마만 알면 된다. 호출부(main.py)가
session.py의 on_event 콜백을 root.after(0, lambda: manager.handle_event(ev))로 감싸서
연결해야 한다(이 클래스 자체는 스레드 세이프하지 않음 — tkinter 메인스레드에서만 호출).
"""

import tkinter as tk
from typing import Callable

from overlay.bubble import events, geometry, shapes
from overlay.bubble.bubble_window import BubbleWindow
from overlay.chat_window import terminal_font_size

FONT_FAMILY = "Noto Sans KR Medium"

_DEFAULTS = {
    "side_gap": 10,
    "anchor_y_ratio": 0.30,
    "width_to_char_ratio": 2.6,
    "min_width": 160,
    "tool_dwell_ms": 1800,
    "tool_stack_max": 3,
    "font_family": FONT_FAMILY,
    "font_size": 0,  # 0 = 자동(TUI 스케일). 양수면 그 크기(px)로 고정.
    # 풍선별 자동 페이드아웃 — 각 타입마다 on/off + 유지 시간(ms). 유지 시간 뒤 페이드로
    # 사라지고, 마우스를 올려두면(hover) 그동안은 안 사라진다(BubbleWindow가 처리).
    "echo_fade": True,       # 입력 에코: 기본 켬(지금처럼 일정 시간 후 사라짐)
    "echo_dwell_ms": 8000,
    "speech_fade": True,     # 응답: 기본 켬(입력처럼 일정 시간 후 사라짐) — turn_end부터 카운트
    "speech_dwell_ms": 20000,
    "thought_fade": True,    # 생각: 기본 켬(turn_end에 바로 정리 — 지금처럼)
    "thought_dwell_ms": 0,   # turn_end 후 이 시간(ms) 뒤 페이드(0 = 즉시)
    # 최대 높이 — 모니터 작업영역 높이 대비 비율. 0이면 무제한.
    "speech_max_height_ratio": 0.55,
    "thought_max_height_ratio": 0.30,
    # 생각풍선에 무엇을 보여줄지.
    #   "full"  = 실제 추론 텍스트를 그대로(없으면 진행 문구로 폴백) — 기존 동작
    #   "brief" = 항상 "생각 중…" 류 진행 문구만 (추론 텍스트가 와도 축약)
    # CLI가 추론 텍스트를 레닥션하고 estimated_tokens만 주는 경우가 있어, full 이어도
    # 환경에 따라 진행 문구만 보일 수 있다. brief 는 그 편차를 없애고 항상 간략하게 만든다.
    "thought_detail": "full",
}

_TOOL_INDICATORS = {"running": "⏳", "ok": "✓", "error": "✗"}
_MIN_RESIZE_WIDTH = 140


class BubbleManager:
    def __init__(
        self,
        root: tk.Tk,
        get_char_rect: Callable[[], tuple[int, int, int, int]],
        cfg_bubble: dict,
        terminal_cfg: dict | None = None,
    ):
        self._root = root
        self._get_char_rect = get_char_rect
        self._terminal_cfg = terminal_cfg or {}
        self._set_cfg(cfg_bubble)

        self._speech = BubbleWindow(root)
        self._speech.set_on_click(self._on_speech_click)
        self._speech.set_on_resize(self._on_speech_resize)
        self._speech.set_on_resize_h(self._on_speech_resize_h)
        self._speech.set_on_move_end(self._on_speech_move_end)
        self._speech.set_on_dismissed(self._on_speech_faded)
        self._speech_text = ""
        self._speech_dismissed = False
        # 능동 발화(initiative nudge)용 오버라이드 — nudge 를 speech 슬롯에 그리되,
        # 색(teal)과 클릭 동작(닫기 대신 대화로 잇기)만 이 두 값으로 갈아끼운다.
        # 실제 어시스턴트 응답이 오거나 새 턴이 시작되면 _clear_nudge_state()로 되돌린다.
        self._speech_on_click_override: "Callable[[], None] | None" = None
        self._speech_color_override: "dict | None" = None
        # 마지막 교환 스냅샷 — 풍선이 페이드로 사라진 뒤에도 캐릭터를 클릭하면 되살릴 수
        # 있게 별도로 보관한다(_speech_text 는 페이드 시 비워지므로 여기 못 씀).
        # _last_was_nudge=True 면 그 발화가 자율발화(사용자 입력 없음)였다는 표시.
        self._last_speech_text = ""
        self._last_was_nudge = False
        # 능동 발화 결과 판정 — 렌더된 nudge 하나에 대해 결과가 아직 안 정해졌으면 True.
        # 반응 없이 사라지는 모든 경로(_settle_nudge_outcome)가 이 잠금을 소비한다.
        self._nudge_outcome_pending = False
        self._nudge_on_ignored: "Callable[[], None] | None" = None
        # 답장 콜백은 결과 판정보다 오래 산다 — 판정이 끝난 뒤에도 캐릭터를 눌러 지난
        # 발화를 되살려 답할 수 있어야 한다. 새 발화나 새 대화 턴이 오면 교체·해제된다.
        self._nudge_reply_cb: "Callable[[], None] | None" = None
        self._speech_width_override: "int | None" = None
        self._speech_max_h_override: "int | None" = None
        self._speech_rect: "tuple[int, int, int, int] | None" = None  # (x, y, w, h) — InputBar가 이 아래로 쌓는 데 씀
        # 사용자가 통짜로 드래그해서 옮긴 뒤의 위치 — "꼬리가 붙어있는 하단 코너"를
        # (char_x/y 대비 char_w/h 비율로) 저장한다. top-left를 고정점으로 쓰면
        # 리사이즈할 때 항상 좌상단이 고정되고 우하단으로만 커져서 캐릭터와 무관하게
        # 따로 노는 것처럼 보인다 — 꼬리(캐릭터를 향하는 쪽) 쪽 하단 코너를 고정점으로
        # 써야 "캐릭터에서 뻗어나온 상태를 유지하며 크기만 바뀌는" 느낌이 난다.
        self._speech_manual_pos: "tuple[float, float] | None" = None
        self._speech_tail_side = "left"  # manual_pos가 어느 쪽 하단 코너를 가리키는지
        # 마지막으로 받은 텍스트 블록의 id(events.py의 "block-{index}") — 같은 턴 안에서도
        # 도구 호출 전/후처럼 블록이 바뀌면 이전 내용을 이어붙이지 않고 새로 시작한다.
        # (지난 응답은 히스토리에서 볼 수 있으니 화면엔 최신 블록만 남겨도 된다.)
        self._speech_block_id: "str | None" = None

        self._thought = BubbleWindow(root)
        self._thought.set_on_click(self._on_thought_click)
        self._thought.set_on_resize(self._on_thought_resize)
        self._thought.set_on_resize_h(self._on_thought_resize_h)
        self._thought.set_on_move_end(self._on_thought_move_end)
        self._thought_text = ""
        self._thought_dismissed = False
        self._thought_rect: "tuple[int, int, int, int] | None" = None  # (x, y, w, h) — 말풍선이 이 위를 비켜가는 데 씀
        self._thought_width_override: "int | None" = None
        self._thought_max_h_override: "int | None" = None
        # 생각풍선은 꼬리가 항상 "down"(하단 중앙)이므로 anchor는 항상 하단-중앙 코너.
        self._thought_manual_pos: "tuple[float, float] | None" = None
        self._thought_block_id: "str | None" = None  # speech와 동일한 이유

        # 에코(사용자 메시지) 슬롯 — 입력창을 제출한 자리에 사용자가 방금 보낸 말을
        # 그대로 남긴다(응답 말풍선과 완전히 별개). 일정 시간(echo_dwell_ms) 유지 후
        # 사라지고, 다음 입력이 들어오면 즉시 교체된다.
        self._echo = BubbleWindow(root)
        self._echo.set_on_click(self._on_echo_click)
        self._echo_text = ""
        self._echo_rect: "tuple[int, int, int, int, str] | None" = None  # (x, y, w, h, tail_side)

        # 도구 상태 줄 — id 순서를 유지하는 리스트 + 내용을 담는 dict.
        self._tool_order: list[str] = []
        self._tool_info: dict[str, dict] = {}

        self._approval_windows: list[BubbleWindow] = []

    def _set_cfg(self, cfg_bubble: dict) -> None:
        self._cfg = {**_DEFAULTS, **(cfg_bubble or {})}
        self._theme = {**shapes.DEFAULT_THEME, **(self._cfg.get("theme") or {})}

    # ── 공개 API ──────────────────────────────────────────────────────

    def update_cfg(self, cfg_bubble: dict) -> None:
        """설정 창에서 저장한 뒤(main.py._reload_config) 색상 테마 등을 즉시 반영한다 —
        안 그러면 프로세스를 재시작해야만 바뀐 설정이 보인다. 지금 떠 있는 풍선이
        있으면 새 테마로 다시 그린다."""
        self._set_cfg(cfg_bubble)
        self._render_thought()  # 내부에서 _render_speech()까지 호출

    def show_user_message(self, text: str) -> None:
        """사용자가 새 입력을 보냈다 = 새 턴 시작. 이전 턴의 생각/도구 상태만 정리하고,
        **사용자의 입력을 응답 말풍선에 에코하지 않는다**.

        예전엔 여기서 입력 텍스트를 응답 말풍선에 그대로 띄웠다가(에코) 어시스턴트
        응답이 오면 교체했는데 — "내가 친 게 응답 자리에 먼저 뜨니 마치 내 입력이
        응답으로 출력되는 것"처럼 보여서 불편하다는 피드백을 받았다. 이제 입력 풍선과
        응답 풍선은 완전히 별개로 동작한다: 입력은 입력대로 닫히고, 응답은 어시스턴트
        첫 조각이 도착할 때 그때 렌더된다. 이전 응답은 그 첫 조각이 올 때까지 그대로
        떠 있다가 block-id가 바뀌며 교체된다(빈 화면 깜빡임 방지).

        text 인자는 API 호환을 위해 남겨두지만 여기서는 쓰지 않는다 — 실제 전송은
        main._on_bubble_submit이 세션으로 직접 보낸다."""
        self._speech.cancel_dismiss()  # 이전 응답의 페이드 예약이 남아 있으면 취소(새 턴 시작)
        self._speech_dismissed = False
        self._thought_dismissed = False
        self._clear_nudge_state()  # 사용자가 턴을 시작했으니 nudge 상태(색/클릭)를 응답용으로 되돌림
        # nudge 를 클릭해서 시작한 턴이면 _on_speech_click 이 이미 잠금을 풀어놨으므로
        # 여기서는 아무 일도 안 한다. 무관한 새 대화를 시작한 경우에만 무시로 마감된다.
        self._settle_nudge_outcome()
        # 새 대화가 시작됐으니 "마지막에 한 말"은 더 이상 자율발화가 아니다 — 답장 경로 해제.
        self._nudge_reply_cb = None
        self._speech_block_id = None  # 다음 speech 델타가 이전 응답을 밀어내고 새로 시작
        self._thought_text = ""
        self._thought_block_id = None
        self._tool_order.clear()
        self._tool_info.clear()
        self._render_thought()  # 생각풍선 상태만 갱신(응답 텍스트는 건드리지 않음)

    def show_echo(self, text: str, input_rect: "tuple[int, int, int, int, str] | None") -> None:
        """사용자가 방금 보낸 메시지를, 입력창이 있던 그 자리(input_rect)에 그대로 남긴다.

        응답 말풍선과 완전히 별개인 "에코" 슬롯 — Claude 앱에서 내가 보낸 말풍선이
        남는 것처럼, 내 입력이 입력창 자리에 잠깐 머문다. echo_dwell_ms 뒤 페이드로
        사라지고, 다음 입력이 들어오면(다시 show_echo 호출) 타이머를 리셋하며 즉시
        교체된다. input_rect은 InputBar가 닫히기 직전의 (x, y, w, h, tail_side)."""
        if not text or input_rect is None:
            return
        self._echo_text = text
        self._echo_rect = input_rect
        self._render_echo()
        # echo_fade가 꺼져 있으면 다음 입력이 올 때까지 유지(자동 페이드 안 함).
        if self._cfg.get("echo_fade", True):
            self._echo.schedule_dismiss(int(self._cfg.get("echo_dwell_ms", 8000)))
        else:
            self._echo.cancel_dismiss()

    def _on_echo_click(self) -> None:
        self._echo_text = ""
        self._echo.hide()

    def _render_echo(self) -> None:
        if not self._echo_text or self._echo_rect is None:
            self._echo.hide()
            return
        x, y, w0, h0, _tail_side = self._echo_rect
        char_x, char_y, char_w, char_h = self._get_char_rect()
        canvas = self._echo.ensure()
        # 입력창과 같은 폭/색을 써서 "방금 그 입력창이 굳어 남은" 느낌 — 응답(speech)
        # 색과 구분되게 input 팔레트를 쓴다.
        fixed_w = max(w0 - shapes.TAIL_REACH * 2, 60)
        font = (self._font_family(), self._font_size())
        angle_rad = geometry.angle_to_point(x + w0 / 2, y + h0 / 2, char_x + char_w / 2, char_y + char_h / 2)
        w, h = shapes.draw_speech_bubble(
            canvas, self._echo_text, fixed_w, angle_rad=angle_rad, font=font, fixed_body_w=fixed_w,
            fg=self._theme["speech_fg"], bg=self._theme["input_bg"], outline=self._theme["input_outline"],
        )
        # 입력창 바닥을 기준으로 위로 자라게 앵커(입력창은 화면 하단에 있었으므로).
        mon_rect = geometry.get_monitor_work_rect(char_x, char_y)
        nx, ny = geometry.clamp_rect(int(x), int(y + h0 - h), w, h, mon_rect)
        self._echo.place(nx, ny, w, h)

    def refresh_positions(self) -> None:
        """새 콘텐츠 없이 지금 떠 있는 풍선들만 캐릭터의 현재 위치 기준으로 다시 배치한다.

        말풍선/생각풍선은 콘텐츠 이벤트가 와야만 다시 그려지므로, 이전 응답이 계속
        떠 있는 상태에서 캐릭터(오버레이)를 다른 곳으로 옮기면 그 풍선들은 캐릭터를
        안 따라가고 옛 위치에 남는다. 캐릭터를 클릭(제자리 클릭, 드래그 아님)해서
        입력창을 열 때마다 호출해서 캐릭터를 따라가게 한다 — 자동 배치든 사용자가
        드래그해서 옮긴 위치든(캐릭터 기준 오프셋이라) 그대로 다시 적용된다."""
        self._render_thought()  # 내부에서 _render_speech()까지 호출(버튼 행 포함)

    def replay_last(self) -> None:
        """캐릭터를 클릭했을 때 마지막 교환을 되살린다 — 풍선이 페이드로 사라졌어도
        가장 최근 응답(+사용자 질문 에코)을 다시 띄운다. 이미 떠 있으면 건드리지 않는다.

        자율발화(_last_was_nudge)였으면 teal 단독으로, 사용자 질문 턴이었으면
        응답(보라)+질문 에코로 복원된다 — 'QA 주체'가 별도 라벨 없이 구성/색만으로
        구분된다(에코 있음=내가 물음, teal 단독=캐릭터가 스스로 말함)."""
        if self._last_speech_text and not self._speech.is_visible():
            self._speech.cancel_dismiss()
            self._speech_dismissed = False
            self._speech_block_id = None
            self._speech_text = self._last_speech_text
            self._speech_color_override = self._nudge_colors() if self._last_was_nudge else None
            # 마지막이 자율발화였다면 답장 경로를 그대로 되살린다 — 사용자가 임의 시점에
            # 캐릭터를 눌러 "마지막에 뭐라고 했더라"를 보고 거기에 답하는 게 상주
            # 캐릭터의 기본 사용법이다. 마지막이 평범한 응답이면 오버라이드 없이
            # (클릭하면 닫히고) 그냥 새 대화를 시작하면 된다.
            self._speech_on_click_override = (
                self._nudge_reply_cb if (self._last_was_nudge and self._nudge_reply_cb) else None
            )
            self._render_speech()  # 버튼 행도 _nudge_reply_cb 유무에 따라 함께 그려진다
            self._speech.schedule_dismiss(int(self._cfg.get("speech_dwell_ms", 20000)))
        if self._echo_text and self._echo_rect is not None and not self._echo.is_visible():
            self._render_echo()
            if self._cfg.get("echo_fade", True):
                self._echo.schedule_dismiss(int(self._cfg.get("echo_dwell_ms", 8000)))

    def handle_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind in ("speech", "thought"):
            self._handle_text_event(ev)
        elif kind == "tool_use":
            self._handle_tool_use(ev)
        elif kind == "tool_result":
            self._handle_tool_result(ev)
        elif kind == "turn_end":
            self._dismiss_thought()  # 생각풍선은 "생성 중 무슨 생각/작업 중인지"가 목적 — 응답이 끝나면 치운다
            self._schedule_speech_fade()  # 응답도 일정 시간 뒤 자동 페이드(설정에 따라)
        elif kind == "error":
            self._handle_error(ev)

    def _schedule_speech_fade(self) -> None:
        """turn_end 후 응답 말풍선을 speech_dwell_ms 뒤 페이드아웃 예약(speech_fade on일 때).
        마우스를 올려두면 그동안은 유지된다(BubbleWindow hover 처리). 페이드가 끝나면
        _on_speech_faded가 상태를 정리해 재등장을 막는다."""
        if self._speech_dismissed or not self._speech_text:
            return
        if self._cfg.get("speech_fade", True):
            self._speech.schedule_dismiss(int(self._cfg.get("speech_dwell_ms", 20000)))

    def _on_speech_faded(self) -> None:
        """응답 말풍선이 페이드로 완전히 사라진 순간 — 상태를 비워서, 이후 캐릭터 클릭
        (refresh_positions) 등으로 다시 그려질 때 사라진 응답이 되살아나지 않게 한다."""
        self._speech_dismissed = True
        self._speech_text = ""
        self._speech_rect = None
        self._clear_nudge_state()  # nudge 가 페이드로 사라진 경우 오버라이드도 정리
        # 반응 없이 dwell 이 만료됐다 = 무시. 유일하게 "무시"를 실제로 관측하는 지점이다
        # (예전엔 발화 시점에 미리 무시로 세느라 이 신호 자체가 필요 없었다).
        self._settle_nudge_outcome()

    def show_approval_request(self, request) -> None:
        """confirm_risky/confirm_always 수준에서 도구 승인이 필요할 때(approval.py의
        ApprovalRequest) 허용/거부 버튼이 있는 풍선을 띄운다. auto 수준에서는 세션이
        이 콜백 자체를 안 부르므로 여기까지 오지 않는다.
        """
        win = BubbleWindow(self._root)
        canvas = win.ensure()
        char_x, char_y, char_w, char_h = self._get_char_rect()
        max_width = self._default_width(char_x, char_y, char_w)
        label = f"{request.tool_name} 실행을 허용할까요?"
        font = (self._font_family(), max(9, self._font_size() - 1))
        body_w, body_h = shapes.draw_tool_bubble(
            canvas, label, "ask", max_width, font=font,
            fg=self._theme["tool_fg"], bg=self._theme["tool_bg"], outline=self._theme["tool_outline"],
        )

        btn_h = 26
        total_h = body_h + btn_h + 6
        canvas.config(height=total_h)

        col_w = max(40, (body_w - 24) // 2)
        allow_btn = tk.Button(canvas, text="허용", command=lambda: self._resolve_approval(win, request, True))
        deny_btn = tk.Button(canvas, text="거부", command=lambda: self._resolve_approval(win, request, False))
        allow_btn.place(x=8, y=body_h + 4, width=col_w, height=btn_h - 4)
        deny_btn.place(x=8 + col_w + 8, y=body_h + 4, width=col_w, height=btn_h - 4)

        x, y, _tail_side, _ = geometry.place_speech_bubble(char_x, char_y, char_w, char_h, body_w, total_h, self._cfg)
        win.place(x, y, body_w, total_h)
        win.schedule_dismiss(65_000)  # approval.py 기본 타임아웃(60s)보다 살짜 여유
        self._approval_windows.append(win)

    def _resolve_approval(self, win: BubbleWindow, request, allow: bool) -> None:
        if allow:
            request.allow()
        else:
            request.deny()
        win.destroy()
        if win in self._approval_windows:
            self._approval_windows.remove(win)

    def clear_all(self) -> None:
        self._settle_nudge_outcome()  # 화면을 통째로 비우는 것도 반응 없이 끝난 경우
        self._nudge_reply_cb = None
        self._speech.hide()
        self._speech_text = ""
        self._speech_dismissed = False
        self._speech_rect = None
        self._clear_nudge_state()
        self._last_speech_text = ""
        self._last_was_nudge = False
        self._speech_manual_pos = None
        self._speech_tail_side = "left"
        self._speech_block_id = None
        self._thought.hide()
        self._thought_text = ""
        self._thought_dismissed = False
        self._thought_rect = None
        self._thought_manual_pos = None
        self._thought_block_id = None
        self._echo.hide()
        self._echo_text = ""
        self._echo_rect = None
        self._tool_order.clear()
        self._tool_info.clear()
        for win in self._approval_windows:
            win.destroy()
        self._approval_windows.clear()

    # ── 대화(speech) 슬롯 ────────────────────────────────────────────

    def show_nudge(
        self,
        text: str,
        on_click: "Callable[[], None]",
        dwell_ms: "int | None" = None,
        on_ignored: "Callable[[], None] | None" = None,
    ) -> None:
        """능동 발화(initiative)를 speech 슬롯에 렌더한다 — 답변과 같은 자리·모양이지만
        teal 색으로 구분하고, 클릭하면 닫히는 대신 on_click(대화로 잇기)이 불린다.
        dwell_ms 뒤 자동 페이드(마우스를 올려두면 유지). 새 턴/응답이 오면 상태가
        응답용으로 되돌아간다(_clear_nudge_state).

        on_ignored: 사용자가 반응하지 않은 채로 이 발화가 끝났을 때 정확히 1회 호출한다
        (dwell 만료 페이드 · "나중에" 버튼 · 무관한 새 턴 시작). 클릭 경로에서는 부르지
        않는다 — 그쪽 결과는 호출부가 답장 여부까지 보고 판정한다."""
        if not text:
            return
        # 앞선 발화가 아직 결과 미확정이면 여기서 무시로 마감한다 — 새 발화가 화면을
        # 덮어쓰면 이전 발화는 영영 판정될 기회가 없다.
        self._settle_nudge_outcome()
        self._nudge_on_ignored = on_ignored
        self._nudge_outcome_pending = True
        self._nudge_reply_cb = on_click  # 판정이 끝난 뒤에도 유지(복원 시 답장용)
        self._speech.cancel_dismiss()
        self._speech_dismissed = False
        self._speech_block_id = None
        self._speech_on_click_override = on_click
        self._speech_color_override = self._nudge_colors()
        self._speech_text = text
        # 자율발화 스냅샷 — 사용자 입력이 없는 턴이므로 직전 사용자 에코를 지워서,
        # 나중에 클릭으로 복원할 때 "teal 단독"(= 캐릭터가 스스로 말함)으로 보이게 한다.
        self._last_speech_text = text
        self._last_was_nudge = True
        self._echo_text = ""
        self._echo_rect = None
        self._echo.hide()
        self._render_speech()  # 버튼 행은 _render_speech 가 풍선과 함께 그린다
        dwell = int(dwell_ms if dwell_ms is not None else self._cfg.get("speech_dwell_ms", 20000))
        self._speech.schedule_dismiss(dwell)

    def _nudge_colors(self) -> dict:
        return dict(
            fg=self._theme.get("nudge_fg", self._theme["speech_fg"]),
            bg=self._theme.get("nudge_bg", self._theme["speech_bg"]),
            outline=self._theme.get("nudge_outline", self._theme["speech_outline"]),
        )

    # ── 능동 발화 결과 판정 ───────────────────────────────────────────

    def _settle_nudge_outcome(self) -> None:
        """결과 미확정 상태로 사라지는 발화를 "무시"로 마감한다 — 페이드/새 턴/교체 등
        사용자가 반응하지 않은 모든 경로가 여기로 모인다. 한 발화당 정확히 1회만
        불리도록 _nudge_outcome_pending 으로 잠근다."""
        if not self._nudge_outcome_pending:
            return
        self._nudge_outcome_pending = False
        cb, self._nudge_on_ignored = self._nudge_on_ignored, None
        self._clear_reply_hint()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # 답장 아이콘 배지 — 말풍선 오른쪽 아래 모서리에 반쯤 걸치게 얹는다.
    _NUDGE_ICON_R = 14          # 배지 반지름
    _NUDGE_ICON_OVERHANG = 16   # 풍선 아래로 삐져나오는 만큼(캔버스를 이만큼 늘린다)
    _NUDGE_ICON_TAG = "nudge_reply"

    def _grow_for_nudge_row(self, canvas, total_w: int, h: int, tail_side: str = "left") -> int:
        """풍선 아래로 배지가 삐져나올 만큼 캔버스를 늘리고 답장 아이콘을 그린다 —
        늘어난 높이 반환. 아이콘이 필요 없으면 높이를 그대로 돌려준다."""
        row_h = self._nudge_row_height()
        if not row_h:
            return h
        bubble_h = h
        h += row_h
        canvas.config(height=h)
        self._draw_reply_hint(canvas, total_w, bubble_h, tail_side)
        return h

    def _nudge_row_height(self) -> int:
        """답장 아이콘을 그려야 하면 캔버스에 덧댈 높이, 아니면 0.

        판정 대기 여부가 아니라 **답장 콜백이 살아있는지**로 정한다. 결과 판정 창
        (dwell 25초)과 답할 수 있는 창은 별개다 — 사용자는 아무 때나 캐릭터를 눌러
        "마지막에 뭐라고 했더라"를 확인하고 그때 답할 수 있어야 한다. 판정 창에
        묶어두면 복원된 발화가 답장 불가가 되고, 직접 타이핑하는 경로엔 발화 문구
        prepend 도 없어서 연속성이 끊긴다."""
        if self._nudge_reply_cb is None:
            return 0
        return self._NUDGE_ICON_OVERHANG

    def _draw_reply_hint(self, canvas, total_w: int, bubble_h: int, tail_side: str = "left") -> None:
        """말풍선 오른쪽 아래 모서리에 SNS 식 REPLY 아이콘을 배지로 그린다.

        tk.Button 두 개(답장/나중에)를 놓았다가 이걸로 교체했다 — 네이티브 버튼은
        풍선의 조형과 전혀 안 맞고, "나중에"는 애초에 필요하지 않다(안 누르고 두면
        그게 곧 무시다).

        **캔버스 아이템으로 그린다**(위젯 아님): 폰트에 화살표 글리프가 있는지에
        의존하지 않고, 임베드 위젯이 캔버스 아이템보다 항상 위에 그려지는 z순서
        문제도 피한다. 클릭 판정은 따로 붙이지 않는다 — 풍선 전체 클릭이 이미
        답장으로 이어지므로(_speech_on_click_override) 이 아이콘은 "눌러도 된다"는
        표시 역할이다. 배지 절반이 투명 영역에 걸치므로 원형 바탕을 깔아야 읽힌다.

        **꼬리 반대쪽 아래 모서리**에 놓는다 — 꼬리는 캐릭터를 향하므로, 같은 쪽에
        놓으면 캐릭터가 오른쪽에 있을 때(기본 배치) 꼬리와 겹친다."""
        r = self._NUDGE_ICON_R
        if tail_side == "right":
            cx = shapes.TAIL_REACH + r
        else:
            cx = total_w - shapes.TAIL_REACH - r
        # 풍선의 "보이는" 바닥은 캔버스 바닥에서 꼬리 여백(TAIL_REACH)만큼 위다 —
        # 캔버스 높이를 기준으로 잡으면 배지가 풍선에서 떨어져 떠 있는 것처럼 보인다.
        cy = bubble_h - shapes.TAIL_REACH
        bg = self._theme.get("nudge_bg", self._theme["speech_bg"])
        line = self._theme.get("nudge_outline", self._theme["speech_outline"])
        fg = self._theme.get("nudge_fg", self._theme["speech_fg"])
        t = self._NUDGE_ICON_TAG
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                           fill=bg, outline=line, width=1.4, tags=t)
        # SNS 답장 화살표 — 오른쪽 아래 꼬리에서 올라와 **수평으로 왼쪽을 향해** 끝난다.
        # 마지막 구간이 수평이어야 화살머리가 정확히 왼쪽을 가리킨다(비스듬하면 마우스
        # 커서처럼 보인다).
        canvas.create_line(
            cx + 6, cy + 6.5, cx + 5.5, cy + 1.5, cx + 2.5, cy - 3,
            cx - 1.5, cy - 3, cx - 7, cy - 3,
            smooth=True, width=1.7, fill=fg, capstyle=tk.ROUND,
            arrow=tk.LAST, arrowshape=(6, 7, 3), tags=t,
        )

    def _clear_reply_hint(self, canvas=None) -> None:
        """아이콘만 지운다 — 다음 렌더의 delete("all") 로도 사라지지만, 풍선을 다시
        그리지 않고 상태만 정리하는 경로(판정 마감 등)에서는 명시적으로 필요하다."""
        target = canvas if canvas is not None else self._speech.canvas
        if target is None:
            return
        try:
            target.delete(self._NUDGE_ICON_TAG)
        except Exception:
            pass

    def _clear_nudge_state(self) -> None:
        self._speech_on_click_override = None
        self._speech_color_override = None
        self._clear_reply_hint()

    def _on_speech_click(self) -> None:
        override = self._speech_on_click_override
        # 클릭은 무시가 아니다 — 마감 콜백이 돌지 않도록 잠금부터 푼다.
        # (_clear_nudge_state → _settle_nudge_outcome 순서로 엮이면 같은 발화가
        #  ignored 로 먼저 집계된 뒤 engaged 가 씹힌다.)
        self._nudge_outcome_pending = False
        self._nudge_on_ignored = None
        self._clear_nudge_state()
        self._speech_dismissed = True
        self._speech.hide()
        self._speech_rect = None  # 숨겼으니 InputBar가 이 자리를 "떠 있는 응답"으로 보고 피할 필요 없음
        if override is not None:
            try:
                override()
            except Exception:
                pass

    def _handle_error(self, ev: dict) -> None:
        # 오류는 중요하므로 사용자가 방금 대화 풍선을 닫았어도 강제로 다시 띄운다.
        self._clear_nudge_state()
        self._settle_nudge_outcome()  # 오류 풍선이 nudge 를 덮으면 그 발화는 판정 기회를 잃는다
        self._speech_dismissed = False
        self._speech_text = f"[오류] {ev.get('text') or ''}"
        self._render_speech()

    def _on_speech_resize(self, new_w: int) -> None:
        char_x, char_y = self._get_char_rect()[:2]
        self._speech_width_override = self._clamp_width(new_w, char_x, char_y)
        self._render_speech()

    def _on_speech_resize_h(self, new_h: int) -> None:
        from overlay.bubble.shapes import TAIL_REACH
        self._speech_max_h_override = max(60, new_h - TAIL_REACH * 2)
        self._render_speech()

    def _on_speech_move_end(self, _dx: int, _dy: int) -> None:
        """본문을 통짜로 드래그해서 옮긴 뒤 — "꼬리가 붙은 하단 코너"를 캐릭터 기준
        비율 오프셋으로 저장해서 다음 렌더(새 턴, 리사이즈 등)에서도 그 코너가
        "캐릭터에서 뻗어나온 자리"로 고정 유지된다(리사이즈는 반대쪽만 움직임).

        절대 픽셀이 아니라 char_w/char_h에 대한 비율로 저장하는 이유: 캐릭터가 다른
        해상도/DPI 모니터로 옮겨지면 character.py가 이미지 크기(char_w/char_h)를 그
        모니터에 맞게 다시 계산한다 — 이때 픽셀 오프셋을 그대로 쓰면 캐릭터가 커진
        모니터에서는 상대적으로 너무 가깝게, 작아진 모니터에서는 너무 멀게 보인다.

        dx/dy(누적 델타)를 더하는 대신 창의 실제 최종 위치를 직접 읽는다 — 드래그
        도중 스트리밍 텍스트가 도착하면 _render_speech()가 위치를 "드래그 중 중간
        위치"로 갱신해두는데, 그 위에 델타를 또 더하면 이동량이 중복 반영된다.

        꼬리 방향은 드래그가 끝난 최종 위치 기준으로 새로 계산한다 — 캐릭터 반대편으로
        옮기면 꼬리도 반대쪽으로 바뀌어야 계속 캐릭터를 향한다."""
        win = self._speech.win
        if win is None or self._speech_rect is None:
            return
        new_x, new_y = win.winfo_x(), win.winfo_y()
        _, _, w, h = self._speech_rect
        char_x, char_y, char_w, char_h = self._get_char_rect()
        tail_side = geometry.tail_side_toward_char(new_x, w, char_x, char_w)
        self._speech_tail_side = tail_side
        anchor_x = new_x if tail_side == "left" else new_x + w
        anchor_y = new_y + h
        self._speech_manual_pos = ((anchor_x - char_x) / char_w, (anchor_y - char_y) / char_h)
        self._speech.set_grip_corner("top-left" if tail_side == "right" else "top-right")
        self._render_speech()  # 꼬리가 바뀌었을 수 있으니 다시 그려서 반영 + rect 갱신

    def is_idle(self) -> bool:
        """화면에 떠 있는 풍선이 하나도 없는지 — initiative 엔진이 "지금 말 걸어도
        되나(화면이 비었나)" 판정할 때 쓴다. 승인 풍선이 떠 있으면 당연히 바쁜 상태."""
        if self._approval_windows:
            return False
        return not (self._speech.is_visible() or self._thought.is_visible() or self._echo.is_visible())

    def get_speech_rect(self) -> "tuple[int, int, int, int] | None":
        """지금 화면에 떠 있는 대화풍선의 (x, y, w, h) — 없으면 None.
        InputBar가 이 아래에 입력창을 쌓을 때 쓴다(같은 자리에 겹쳐서 응답을 가리던 문제 방지)."""
        return self._speech_rect

    def _render_speech(self) -> None:
        if self._speech_dismissed or not self._speech_text:
            self._speech.hide()
            self._speech_rect = None
            return
        char_x, char_y, char_w, char_h = self._get_char_rect()
        max_width = self._speech_width_override or self._default_width(char_x, char_y, char_w)
        fixed_w = self._speech_width_override
        font = (self._font_family(), self._font_size())
        canvas = self._speech.ensure()
        speech_colors = self._speech_color_override or dict(
            fg=self._theme["speech_fg"], bg=self._theme["speech_bg"], outline=self._theme["speech_outline"],
        )
        max_body_h = (self._speech_max_h_override if self._speech_max_h_override is not None
                      else self._max_body_h(char_y, "speech_max_height_ratio"))
        # 각도는 아직 모르니 임시값(0)으로 그려서 크기만 잰다(폭/높이는 각도와 무관).
        w, h = shapes.draw_speech_bubble(canvas, self._speech_text, max_width, font=font, fixed_body_w=fixed_w, max_body_h=max_body_h, **speech_colors)
        h += self._nudge_row_height()  # 버튼 행까지 포함한 높이로 위치를 잡아야 한다

        if self._speech.is_moving():
            # 사용자가 지금 이 풍선을 드래그/리사이즈하는 중 — 스트리밍 중 도착한 텍스트가
            # 위치를 자동 계산값으로 덮어써서 드래그와 충돌하면(창이 마우스 아래에서
            # 튕겨나감) release 시점에 델타 계산이 틀어져 저장되는 위치 자체가 잘못된다.
            # 콘텐츠 크기만 맞추고 위치는 절대 건드리지 않는다.
            win = self._speech.win
            if win is not None:
                cur_x, cur_y = win.winfo_x(), win.winfo_y()
                tail_side = geometry.tail_side_toward_char(cur_x, w, char_x, char_w)
                grip_corner = "top-left" if tail_side == "right" else "top-right"
                angle_rad = geometry.angle_to_point(
                    cur_x + w / 2, cur_y + h / 2, char_x + char_w / 2, char_y + char_h / 2
                )
                w, h = shapes.draw_speech_bubble(
                    canvas, self._speech_text, max_width, angle_rad=angle_rad, font=font,
                    fixed_body_w=fixed_w, grip_corner=grip_corner, max_body_h=max_body_h, **speech_colors,
                )
                h = self._grow_for_nudge_row(canvas, w, h, tail_side)
                win.geometry(f"{w}x{h}")
                self._speech_rect = (cur_x, cur_y, w, h)
            return

        mon_rect = geometry.get_monitor_work_rect(char_x, char_y)
        if self._speech_manual_pos is not None:
            # 드래그로 옮긴 뒤에는 꼬리 방향을 이 렌더에서 다시 판단하지 않고, 마지막
            # 드래그가 끝났을 때 정한 방향(_speech_tail_side)을 그대로 쓴다 — 텍스트
            # 길이 변화 등으로 중심선을 살짝 넘나들 때마다 꼬리가 깜빡이며 바뀌는 걸 막고,
            # "꼬리가 붙은 하단 코너"를 고정점으로 리사이즈가 자연스럽게 되게 한다.
            tail_side = self._speech_tail_side
            mx, my = self._speech_manual_pos
            anchor_x = char_x + mx * char_w
            anchor_y = char_y + my * char_h
            x = anchor_x if tail_side == "left" else anchor_x - w
            y = anchor_y - h
            x, y = geometry.clamp_rect(int(x), int(y), w, h, mon_rect)
        else:
            x, y, _unused_tail, mon_rect = geometry.place_speech_bubble(char_x, char_y, char_w, char_h, w, h, self._cfg)
            x, y = self._avoid_thought_overlap(x, y, w, h, mon_rect)
            # 자동 배치일 때는 최종 위치 기준으로 매번 다시 판단 — 자유롭게 캐릭터를 따라간다.
            tail_side = geometry.tail_side_toward_char(x, w, char_x, char_w)
            self._speech_tail_side = tail_side  # 나중에 드래그를 시작할 때 이 값을 물려받음

        grip_corner = "top-left" if tail_side == "right" else "top-right"
        self._speech.set_grip_corner(grip_corner)
        # 꼬리는 항상 몸통 중심→캐릭터 중심 방향의 실제 각도를 향한다(좌우 이진이 아니라
        # 대각선도 정확히 표현) — grip_corner/리사이즈 앵커만 tail_side(이진)를 그대로 쓴다.
        angle_rad = geometry.angle_to_point(x + w / 2, y + h / 2, char_x + char_w / 2, char_y + char_h / 2)
        w, h = shapes.draw_speech_bubble(
            canvas, self._speech_text, max_width, angle_rad=angle_rad, font=font,
            fixed_body_w=fixed_w, grip_corner=grip_corner, max_body_h=max_body_h, **speech_colors,
        )
        h = self._grow_for_nudge_row(canvas, w, h, tail_side)

        self._speech.place(x, y, w, h)
        self._speech_rect = (x, y, w, h)

    def _avoid_thought_overlap(
        self, x: int, y: int, w: int, h: int, mon_rect: tuple[int, int, int, int]
    ) -> tuple[int, int]:
        """생각풍선은 항상 캐릭터 머리 위에 고정이므로, 말풍선이 그 영역을 침범하면
        말풍선 쪽을 더 위로 밀어낸다(생각풍선을 옮기면 "머리 위" 원칙이 깨짐)."""
        if self._thought_rect is None:
            return x, y
        t_x, t_y, t_w, t_h = self._thought_rect
        gap = 8
        # 새 배치(말풍선=캐릭터 옆, 생각풍선=머리 위)에서는 둘이 대개 안 겹친다. 살짝
        # 스치는 정도로 말풍선을 위로 확 밀어올리면 "읽던 응답이 갑자기 위로 점프"해서
        # 오히려 거슬린다(사용자 피드백) — 가로/세로 둘 다 실질적으로 크게 겹칠 때만
        # 비켜준다(겹침 면적이 각 풍선 폭/높이의 상당 부분일 때).
        overlap_x = min(x + w, t_x + t_w) - max(x, t_x)
        overlap_y = (y + h) - (t_y - gap)
        substantial = overlap_x > min(w, t_w) * 0.5 and overlap_y > min(h, t_h) * 0.4
        if substantial:
            y = t_y - gap - h
            _, y = geometry.clamp_rect(x, y, w, h, mon_rect)
        return x, y

    # ── 생각(thought) + 도구 슬롯 ─────────────────────────────────────

    def _dismiss_thought(self) -> None:
        """응답 생성이 끝나면(turn_end) 생각풍선을 정리한다 — "지금 무슨 생각/작업 중인지"가
        목적이라 생성이 끝나면 볼 이유가 없다. thought_fade가 꺼져 있으면 다음 턴/클릭
        때까지 유지하고, 켜져 있으면 thought_dwell_ms(기본 0=즉시) 뒤 페이드아웃한다."""
        if not self._cfg.get("thought_fade", True):
            return  # 유지 — 다음 show_user_message나 클릭에서 정리됨
        self._thought_dismissed = True
        self._thought_block_id = None
        self._tool_order.clear()
        self._tool_info.clear()
        self._thought_rect = None  # 말풍선이 이 자리를 더는 비켜갈 필요 없음
        # 즉시 hide 대신 페이드아웃 — dwell(기본 0) 뒤 부드럽게 사라진다.
        self._thought.schedule_dismiss(int(self._cfg.get("thought_dwell_ms", 0)))
        # 말풍선은 여기서 다시 그리지 않는다(생성 중 위치 튐 방지) — 자기 내용이 바뀔
        # 때만 그 시점의 생각풍선 자리를 보고 스스로 피한다.

    def _on_thought_click(self) -> None:
        self._thought_dismissed = True
        self._thought.hide()
        self._thought_rect = None
        self._render_speech()  # 생각풍선이 닫혔으니 말풍선을 다시 원래 자리로

    def _on_thought_resize(self, new_w: int) -> None:
        char_x, char_y = self._get_char_rect()[:2]
        self._thought_width_override = self._clamp_width(new_w, char_x, char_y)
        self._render_thought()

    def _on_thought_resize_h(self, new_h: int) -> None:
        from overlay.bubble.shapes import TAIL_REACH
        self._thought_max_h_override = max(60, new_h - TAIL_REACH * 2)
        self._render_thought()

    def _on_thought_move_end(self, _dx: int, _dy: int) -> None:
        # 꼬리(down)가 붙은 하단-중앙 코너를 고정점으로 저장 — speech와 동일한 이유로
        # 리사이즈가 그 코너를 고정하고 반대쪽(위쪽)만 움직이게 한다.
        win = self._thought.win
        if win is None or self._thought_rect is None:
            return
        new_x, new_y = win.winfo_x(), win.winfo_y()
        _, _, w, h = self._thought_rect
        char_x, char_y, char_w, char_h = self._get_char_rect()
        anchor_x = new_x + w / 2
        anchor_y = new_y + h
        self._thought_manual_pos = ((anchor_x - char_x) / char_w, (anchor_y - char_y) / char_h)
        self._render_thought()

    def _handle_text_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        text = ev.get("text") or ""
        block_id = ev.get("id")
        is_delta = bool(ev.get("delta"))

        if kind == "speech":
            self._speech.cancel_dismiss()  # 새 응답 조각이 오는 중 — 이전 페이드 예약 취소
            self._clear_nudge_state()  # 실제 어시스턴트 응답 — nudge 색/클릭 오버라이드 해제
            self._nudge_reply_cb = None  # 마지막에 한 말이 응답으로 바뀌었으니 답장 경로도 해제
            self._speech_dismissed = False
            if is_delta and block_id is not None and block_id != self._speech_block_id:
                # 블록이 바뀌면(새 턴의 첫 조각, 또는 같은 턴 안에서 도구 호출 전/후로
                # 나뉜 응답) 이전 블록 내용을 이어붙이지 않고 새로 시작한다 — 새 턴이면
                # 이 시점에 이전 턴의 응답이 밀려나고 새 응답이 그 자리를 차지한다(에코 없이
                # 곧바로 어시스턴트 응답으로 교체). 지난 응답은 히스토리로 볼 수 있다.
                self._speech_text = ""
            if block_id is not None:
                self._speech_block_id = block_id
            self._speech_text = self._speech_text + text if is_delta else text
            # 마지막 응답 스냅샷 — 사용자 질문이 있었던 턴이므로 nudge 아님.
            self._last_speech_text = self._speech_text
            self._last_was_nudge = False
            self._render_speech()
        else:  # "thought"
            if is_delta and block_id is not None and block_id != self._thought_block_id:
                self._thought_text = ""
            if block_id is not None:
                self._thought_block_id = block_id
            self._thought_text = self._thought_text + text if is_delta else text
            self._render_thought()

    def _handle_tool_use(self, ev: dict) -> None:
        tool_id = ev.get("id")
        self._tool_info[tool_id] = {"name": ev.get("tool_name") or "tool", "status": "running"}
        if tool_id not in self._tool_order:
            self._tool_order.append(tool_id)
        while len(self._tool_order) > int(self._cfg["tool_stack_max"]):
            oldest = self._tool_order.pop(0)
            self._tool_info.pop(oldest, None)
        self._render_thought()

    def _handle_tool_result(self, ev: dict) -> None:
        tool_id = ev.get("id")
        if tool_id not in self._tool_info:
            return
        self._tool_info[tool_id]["status"] = "error" if ev.get("is_error") else "ok"
        self._render_thought()
        self._root.after(int(self._cfg["tool_dwell_ms"]), lambda tid=tool_id: self._expire_tool_line(tid))

    def _expire_tool_line(self, tool_id: str) -> None:
        if tool_id in self._tool_info:
            self._tool_info.pop(tool_id, None)
        if tool_id in self._tool_order:
            self._tool_order.remove(tool_id)
        self._render_thought()

    def _format_tool_line(self, tool_id: str) -> str:
        info = self._tool_info[tool_id]
        indicator = _TOOL_INDICATORS.get(info["status"], "⏳")
        return f"{indicator} {info['name']}"

    def _tool_lines_text(self) -> str:
        return "\n".join(self._format_tool_line(tid) for tid in self._tool_order)

    def _thought_display_text(self) -> str:
        """생각풍선에 실제로 그릴 텍스트 — bubble.thought_detail 설정을 적용한다.

        원문(self._thought_text)은 그대로 두고 표시할 때만 축약하므로, 설정을 바꾸면
        (update_cfg → 재렌더) 스트리밍을 다시 받지 않아도 즉시 반영된다."""
        text = self._thought_text
        if not text:
            return text
        if str(self._cfg.get("thought_detail", "full")).strip().lower() == "brief":
            return events.brief_thinking_text(text)
        return text

    def _render_thought(self) -> None:
        tool_lines = self._tool_lines_text()
        if self._thought_dismissed or not (self._thought_text or tool_lines):
            self._thought.hide()
            self._thought_rect = None
            self._render_speech()  # 생각풍선이 사라졌으니 말풍선이 비켜갈 이유도 없어짐
            return
        thought_text = self._thought_display_text()
        char_x, char_y, char_w, char_h = self._get_char_rect()
        max_width = self._thought_width_override or self._default_width(char_x, char_y, char_w)
        font = (self._font_family(), max(9, self._font_size() - 1))
        canvas = self._thought.ensure()
        thought_colors = dict(
            fg=self._theme["thought_fg"], bg=self._theme["thought_bg"], outline=self._theme["thought_outline"],
            tool_fg=self._theme["thought_tool_fg"],
        )
        max_body_h = (self._thought_max_h_override if self._thought_max_h_override is not None
                      else self._max_body_h(char_y, "thought_max_height_ratio"))
        # 각도는 아직 모르니 임시값(기본=위쪽)으로 그려서 크기만 잰다(폭/높이는 각도와 무관).
        w, h = shapes.draw_thought_bubble(
            canvas, thought_text, max_width, font=font,
            fixed_body_w=self._thought_width_override, tool_lines=tool_lines, max_body_h=max_body_h, **thought_colors,
        )

        if self._thought.is_moving():
            # speech와 동일한 이유 — 드래그 중에는 위치를 건드리지 않고 크기만 맞춘다.
            win = self._thought.win
            if win is not None:
                cur_x, cur_y = win.winfo_x(), win.winfo_y()
                angle_rad = geometry.angle_to_point(
                    cur_x + w / 2, cur_y + h / 2, char_x + char_w / 2, char_y + char_h / 2
                )
                w, h = shapes.draw_thought_bubble(
                    canvas, thought_text, max_width, angle_rad=angle_rad, font=font,
                    fixed_body_w=self._thought_width_override, tool_lines=tool_lines, max_body_h=max_body_h, **thought_colors,
                )
                win.geometry(f"{w}x{h}")
                self._thought_rect = (cur_x, cur_y, w, h)
            self._render_speech()
            return

        if self._thought_manual_pos is not None:
            # 하단-중앙 코너를 고정점으로 — 리사이즈해도 그 점을 중심으로 좌우/위로만
            # 커진다(항상 그 지점에서 뻗어나온 것처럼 보임). 꼬리의 실제 각도는 아래에서
            # 매번 캐릭터 중심 방향으로 다시 계산하므로, 이 고정점은 순수 리사이즈
            # 앵커일 뿐 시각적 꼬리 방향과는 분리되어 있다.
            mx, my = self._thought_manual_pos
            mon_rect = geometry.get_monitor_work_rect(char_x, char_y)
            anchor_x = char_x + mx * char_w
            anchor_y = char_y + my * char_h
            x, y = geometry.clamp_rect(int(anchor_x - w / 2), int(anchor_y - h), w, h, mon_rect)
        else:
            x, y, _ = geometry.place_thought_bubble(char_x, char_y, char_w, char_h, w, h, self._cfg)

        # 꼬리는 항상 몸통 중심→캐릭터 중심 방향의 실제 각도를 향한다(대각선 포함).
        angle_rad = geometry.angle_to_point(x + w / 2, y + h / 2, char_x + char_w / 2, char_y + char_h / 2)
        w, h = shapes.draw_thought_bubble(
            canvas, thought_text, max_width, angle_rad=angle_rad, font=font,
            fixed_body_w=self._thought_width_override, tool_lines=tool_lines, max_body_h=max_body_h, **thought_colors,
        )
        self._thought.place(x, y, w, h)
        self._thought_rect = (x, y, w, h)
        # 말풍선은 여기서 다시 그리지 않는다(_dismiss_thought의 설명과 동일한 이유) —
        # 생각풍선이 스트리밍 중 계속 자라날 때마다 말풍선이 매번 그 자리를 피해
        # 위로 밀려 올라가면 응답 생성 내내 말풍선이 계속 움직이는 것처럼 보인다.
        # 말풍선은 자기 콘텐츠 이벤트가 올 때만 그 순간의 생각풍선 자리를 보고 스스로
        # 피한다(_handle_text_event → _render_speech).

    # ── 공용 ──────────────────────────────────────────────────────────

    def _default_width(self, char_x: int, char_y: int, char_w: int) -> int:
        return geometry.default_bubble_width(char_x, char_y, char_w, self._cfg)

    def _clamp_width(self, width: int, char_x: int, char_y: int) -> int:
        mon = geometry.get_monitor_work_rect(char_x, char_y)
        max_w = int((mon[2] - mon[0]) * 0.9)
        return max(_MIN_RESIZE_WIDTH, min(width, max_w))

    def _max_body_h(self, char_y: int, ratio_key: str) -> "int | None":
        ratio = float(self._cfg.get(ratio_key) or 0)
        if ratio <= 0:
            return None
        char_x = self._get_char_rect()[0]
        _, mt, _, mb = geometry.get_monitor_work_rect(char_x, char_y)
        return max(60, int((mb - mt) * ratio))

    def _font_family(self) -> str:
        return self._cfg.get("font_family") or FONT_FAMILY

    def _font_size(self) -> int:
        override = self._cfg.get("font_size") or 0
        if override:  # 사용자가 설정에서 고정 크기를 지정 — TUI 스케일 무시
            return max(8, int(override))
        char_x, char_y = self._get_char_rect()[:2]
        return round(terminal_font_size(char_x, char_y, self._terminal_cfg))
