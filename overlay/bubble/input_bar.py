"""캐릭터 클릭 시 뜨는 입력 풍선 — 말풍선 모양 안에 실제 tk.Entry를 얹는다.

Canvas가 배경 도형을 그리고 그 위 특정 좌표에 진짜 위젯을 place()하는 건 tkinter에서
표준적으로 잘 동작한다(진짜 alpha 필요 없음 — Entry 자체는 불투명해도 문제없다).

"바깥 클릭으로 닫기"는 우리 앱 창 밖(데스크탑/다른 앱)의 클릭은 애초에 tkinter
이벤트로 안 잡히므로 범위에서 뺐다 — Enter(전송)/Escape(취소)로 충분히 가볍다.
"""

import tkinter as tk
from typing import Callable, Optional

from overlay.bubble import geometry, shapes
from overlay.bubble.bubble_window import BubbleWindow
from overlay.chat_window import terminal_font_size

PLACEHOLDER = "말 걸어보기..."
_MIN_RESIZE_WIDTH = 140
FONT_FAMILY = "Noto Sans KR Medium"


class InputBar:
    def __init__(
        self,
        root: tk.Tk,
        get_char_rect: Callable[[], tuple[int, int, int, int]],
        cfg_bubble: dict,
        terminal_cfg: "dict | None" = None,
        get_speech_rect: "Callable[[], tuple[int, int, int, int] | None] | None" = None,
    ):
        self._root = root
        self._get_char_rect = get_char_rect
        self._cfg = cfg_bubble or {}
        self._terminal_cfg = terminal_cfg or {}
        self._theme = {**shapes.DEFAULT_THEME, **(self._cfg.get("theme") or {})}
        self._get_speech_rect = get_speech_rect or (lambda: None)
        self._bubble = BubbleWindow(root)
        self._bubble.set_on_resize(self._on_resize)
        self._bubble.set_on_move_end(self._on_move_end)
        self._entry: Optional[tk.Entry] = None
        self._on_submit: Optional[Callable[[str], None]] = None
        self._on_close: Optional[Callable[[], None]] = None
        self._width_override: "int | None" = None  # 사용자가 grip으로 드래그한 폭 — 다음에 열 때도 유지
        self._body_h = 0
        # 사용자가 통짜로 드래그해서 옮긴 뒤 "꼬리가 붙은 하단 코너"의 비율 오프셋 —
        # bubble_manager.py의 speech와 동일한 이유(리사이즈가 top-left를 고정점으로
        # 쓰면 항상 좌상단이 고정되고 우하단으로만 커지는 문제를 피하기 위함).
        self._manual_pos: "tuple[float, float] | None" = None
        self._tail_side = "left"  # manual_pos가 어느 쪽 하단 코너를 가리키는지

    def update_cfg(self, cfg_bubble: dict) -> None:
        """설정 창 저장 후(main.py._reload_config) 색상 테마 등을 즉시 반영한다 — 안
        그러면 프로세스를 재시작해야만 바뀐 설정이 보인다."""
        self._cfg = cfg_bubble or {}
        self._theme = {**shapes.DEFAULT_THEME, **(self._cfg.get("theme") or {})}
        if self.is_showing():
            char_x, char_y, char_w, _char_h = self._get_char_rect()
            body_w = self._width_override or geometry.default_bubble_width(char_x, char_y, char_w, self._cfg)
            self._layout(body_w, self._body_h)

    def get_manual_pos(self) -> "tuple[float, float] | None":
        """사용자가 입력창을 직접 옮긴 적이 있으면 그 비율 오프셋(꼬리가 붙은 하단
        코너 기준) — main.py가 입력 확정 시 응답(말풍선)이 같은 자리에서 이어지도록
        넘겨주는 데 쓴다. 반드시 get_tail_side()와 같이 넘겨야 한다 — 오프셋은 "어느
        쪽 코너"인지와 묶여 있어서, 말풍선 쪽 tail_side를 안 맞추면 반대쪽 코너로
        해석돼 엉뚱한 자리에 나타난다."""
        return self._manual_pos

    def clear_manual_pos(self) -> None:
        """입력을 확정해 응답 풍선이 이 위치를 이어받은 뒤 호출 — 그러지 않으면 이
        위치가 영구히 남아서, 사용자가 응답 풍선을 직접 다른 곳으로 옮겨도 다음 턴에
        또 이 낡은 입력창 위치로 되돌아오는 문제가 있었다("입력창을 옮긴 적 있으면
        응답을 아무리 옮겨도 항상 그 자리로 돌아온다"). 다음 입력창은 다시 자동 배치
        (또는 새로 드래그한 자리)를 따른다."""
        self._manual_pos = None

    def get_tail_side(self) -> str:
        """get_manual_pos()가 가리키는 코너가 어느 쪽(꼬리 방향)인지 — 같이 넘겨야 한다."""
        return self._tail_side

    def is_showing(self) -> bool:
        return self._entry is not None

    def show(
        self,
        on_submit: Callable[[str], None],
        on_close: "Optional[Callable[[], None]]" = None,
    ) -> None:
        """on_close: 아무것도 보내지 않고 입력창이 닫힌 순간 1회 호출(Escape·재클릭 등).
        제출로 닫히는 경우에는 부르지 않는다 — 호출부가 "열어보고 그냥 닫았다"와
        "실제로 답장했다"를 구분해야 하기 때문.

        입력창은 항상 **비어서** 열린다. 자율발화 답장 경로에서 문구를 미리 채워봤지만,
        그것도 "내 의견 없이 문장이 자동 생성되는" 것이어서 되돌렸다."""
        if self.is_showing():
            self.hide()
            return
        self._on_submit = on_submit
        self._on_close = on_close
        char_x, char_y, char_w, _char_h = self._get_char_rect()
        canvas = self._bubble.ensure()

        size_override = int(self._cfg.get("font_size") or 0)
        font_size = max(8, size_override) if size_override else round(terminal_font_size(char_x, char_y, self._terminal_cfg))
        font = (self._cfg.get("font_family") or FONT_FAMILY, font_size)
        body_w = self._width_override or geometry.default_bubble_width(char_x, char_y, char_w, self._cfg)
        self._body_h = int(font_size * 1.8) + shapes.PADDING * 2

        # Entry 배경을 입력 말풍선 채움색과 정확히 맞춰서, Pillow로 그린 평면 몸통
        # 위에 얹었을 때 이음새(밝기 차이 사각형)가 안 보이게 한다.
        entry_bg = self._theme["input_bg"]
        self._entry = tk.Entry(
            canvas, font=font, relief="flat", bd=0,
            bg=entry_bg, fg=shapes.SPEECH_FG, insertbackground=shapes.SPEECH_FG,
            highlightthickness=0,
        )
        self._entry.insert(0, "")
        self._layout(body_w, self._body_h)
        self._entry.focus_set()
        self._entry.bind("<Return>", self._on_enter)
        self._entry.bind("<Escape>", lambda _e: self.hide())

    def _layout(self, body_w: int, body_h: int) -> None:
        """말풍선 껍데기를 (다시) 그리고 그 위의 Entry 위치/크기를 맞춘다 — 드래그 리사이즈
        중에도 Entry를 destroy하지 않고 이 메서드만 반복 호출해서 포커스/입력값을 지킨다.

        지금 화면에 대화풍선(응답)이 떠 있으면 그 자리를 덮지 않도록 바로 아래에 쌓는다
        — 안 그러면 응답을 보면서 후속 질문을 입력하려고 할 때 입력창이 응답을 가려버린다.
        단, 사용자가 직접 드래그해서 옮긴 적이 있으면 그 자리를 그대로 지킨다(자동 배치/
        스택 로직은 건너뜀)."""
        canvas = self._bubble.ensure()
        char_x, char_y, char_w, char_h = self._get_char_rect()
        input_colors = dict(bg=self._theme["input_bg"], outline=self._theme["input_outline"])
        # 각도는 아직 모르니 임시값(0)으로 그려서 크기만 잰다(폭/높이는 각도와 무관).
        w, h = shapes.draw_input_shell(canvas, body_w, body_h, **input_colors)

        mon_rect = geometry.get_monitor_work_rect(char_x, char_y)
        if self._manual_pos is not None:
            # 마지막 드래그가 끝났을 때 정한 방향을 그대로 쓴다(리사이즈 중 꼬리가
            # 깜빡이며 바뀌는 것 방지 + 꼬리가 붙은 코너를 고정점으로 리사이즈되게).
            tail_side = self._tail_side
            mx, my = self._manual_pos
            anchor_x = char_x + mx * char_w
            anchor_y = char_y + my * char_h
            x = anchor_x if tail_side == "left" else anchor_x - w
            y = anchor_y - h
            x, y = geometry.clamp_rect(int(x), int(y), w, h, mon_rect)
        else:
            # 기본 위치 = 캐릭터 옆 '하단'(place_input_default). 응답 풍선은 캐릭터 옆
            # '상단'에서 위로 자라므로 기본 상태에서 둘이 세로로 분리된다("응답이 입력
            # 자리에서 시작"하던 문제 해결). 응답이 이미 떠 있으면 그 바로 아래에 쌓되,
            # 화면 밖으로 넘치면 하단 기본 위치로 되돌린다.
            x, y, _t = geometry.place_input_default(char_x, char_y, char_w, char_h, w, h, self._cfg)
            speech_rect = self._get_speech_rect()
            if speech_rect is not None:
                sx, sy, sw, sh = speech_rect
                # 캐릭터가 오른쪽에 있으면(꼬리가 오른쪽) 두 풍선의 오른쪽 가장자리를
                # 맞춰 쌓는다 — 왼쪽(sx) 기준으로 맞추면 입력창 폭이 응답풍선보다 넓을
                # 때 캐릭터 쪽으로 더 삐져나와 겹친다. 캐릭터가 왼쪽이면 반대로 왼쪽을 맞춘다.
                speech_tail_side = geometry.tail_side_toward_char(sx, sw, char_x, char_w)
                stack_x = sx + sw - w if speech_tail_side == "right" else sx
                if sy + sh + 8 + h <= mon_rect[3]:  # 아래에 쌓을 자리가 있을 때만
                    x, y = geometry.clamp_rect(stack_x, sy + sh + 8, w, h, mon_rect)
            tail_side = geometry.tail_side_toward_char(x, w, char_x, char_w)
            self._tail_side = tail_side

        grip_corner = "top-left" if tail_side == "right" else "top-right"
        self._bubble.set_grip_corner(grip_corner)
        # 입력은 사용자의 발화이므로 캐릭터가 아니라, 캐릭터가 있는 디스플레이의
        # 실제 마지막 픽셀 행 중앙을 향하게 해 응답 풍선과 화자 방향을 구분한다.
        monitor_rect = geometry.get_monitor_rect(char_x + char_w // 2, char_y + char_h // 2)
        target_x, target_y = geometry.monitor_bottom_center_pixel(monitor_rect)
        angle_rad = geometry.angle_to_point(x + w / 2, y + h / 2, target_x, target_y)
        w, h = shapes.draw_input_shell(canvas, body_w, body_h, angle_rad=angle_rad, grip_corner=grip_corner, **input_colors)

        self._bubble.place(x, y, w, h)
        self._bubble.cancel_dismiss()

        # 몸통은 이제 상하좌우 대칭인 TAIL_REACH 여백 안쪽에서 시작한다(꼬리가 각도에
        # 따라 어느 방향으로도 삐져나올 수 있어, 더 이상 tail_side로 좌우를 다르게 줄
        # 필요가 없다) — 위/아래 여백도 반드시 TAIL_REACH를 더해야 한다. PADDING만
        # 쓰면 Entry가 실제로 그려진 몸통(둥근 사각형) 경계보다 위/아래로 삐져나와
        # 투명 글로우/그립 영역과 겹쳐서 구석에 이상한 조각이 보이는 버그가 된다.
        pad = shapes.TAIL_REACH + shapes.PADDING
        if self._entry is not None:
            self._entry.place(
                x=pad,
                y=pad,
                width=max(20, w - pad * 2),
                height=max(16, h - pad * 2),
            )

    def _on_resize(self, new_w: int) -> None:
        if self._entry is None:
            return
        char_x, char_y = self._get_char_rect()[:2]
        mon = geometry.get_monitor_work_rect(char_x, char_y)
        max_w = int((mon[2] - mon[0]) * 0.9)
        body_w = max(_MIN_RESIZE_WIDTH, min(new_w, max_w))
        self._width_override = body_w
        self._layout(body_w, self._body_h)

    def _on_move_end(self, dx: int, dy: int) -> None:
        if self._bubble.win is None:
            return
        char_x, char_y, char_w, char_h = self._get_char_rect()
        new_x, new_y = self._bubble.win.winfo_x(), self._bubble.win.winfo_y()
        w = self._bubble.win.winfo_width()
        tail_side = geometry.tail_side_toward_char(new_x, w, char_x, char_w)
        self._tail_side = tail_side
        anchor_x = new_x if tail_side == "left" else new_x + w
        anchor_y = new_y + self._bubble.win.winfo_height()
        self._manual_pos = ((anchor_x - char_x) / char_w, (anchor_y - char_y) / char_h)
        grip_corner = "top-left" if tail_side == "right" else "top-right"
        self._bubble.set_grip_corner(grip_corner)
        self._layout(self._width_override or geometry.default_bubble_width(char_x, char_y, char_w, self._cfg), self._body_h)

    def _on_enter(self, _event) -> None:
        text = self._entry.get().strip() if self._entry is not None else ""
        if not text:
            return
        # 닫히기 직전의 입력창 위치를 붙잡아 둔다 — 제출 후 이 자리에 "에코(내 메시지)"
        # 말풍선을 남기는 데 쓴다(BubbleManager.show_echo). 반드시 hide() 전에 읽어야 함.
        self._last_rect = self.current_rect()
        callback = self._on_submit
        self._on_close = None  # 제출로 닫히는 건 "그냥 닫음"이 아니다
        self.hide()
        if callback is not None:
            callback(text)

    def current_rect(self) -> "tuple[int, int, int, int, str] | None":
        """지금 떠 있는 입력창의 (x, y, w, h, tail_side) — 닫혀 있으면 None."""
        win = self._bubble.win
        if win is None:
            return None
        try:
            return (win.winfo_x(), win.winfo_y(), win.winfo_width(), win.winfo_height(), self._tail_side)
        except Exception:
            return None

    def get_last_rect(self) -> "tuple[int, int, int, int, str] | None":
        """마지막 제출 시점의 입력창 위치 — main._on_bubble_submit이 에코 배치에 쓴다."""
        return getattr(self, "_last_rect", None)

    def hide(self) -> None:
        was_showing = self._entry is not None
        if self._entry is not None:
            try:
                self._entry.destroy()
            except Exception:
                pass
            self._entry = None
        self._on_submit = None
        on_close, self._on_close = self._on_close, None
        self._bubble.hide()
        if was_showing and on_close is not None:
            try:
                on_close()
            except Exception:
                pass
