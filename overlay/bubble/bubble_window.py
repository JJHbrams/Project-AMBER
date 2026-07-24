"""풍선 1개의 생명주기 — Toplevel+Canvas, ephemeral 타이머, fade-out.

캐릭터 창(character.py)과 동일한 chroma-key 투명 방식. 진짜 alpha 페이드는 Windows
tkinter에서 안 되므로, fade는 Canvas.scale로 내용을 중심 기준 축소하며 창도 같이
줄여 "사라지는" 느낌을 낸다.
"""

import tkinter as tk
from typing import Callable, Optional

from overlay.bubble.shapes import GRIP_SIZE, TAIL_REACH

_CHROMA = "#010101"


class BubbleWindow:
    def __init__(self, root: tk.Tk):
        self._root = root
        self.win: "tk.Toplevel | None" = None
        self.canvas: "tk.Canvas | None" = None
        self._dismiss_after_id: "str | None" = None
        self._fade_after_id: "str | None" = None
        self._hover = False
        self._pending_dwell_ms: "int | None" = None
        self.tag: "str | None" = None  # 호출부가 매칭용으로 쓰는 임의 식별자(예: tool_use id)
        self._on_click: Optional[Callable[[], None]] = None
        self._on_resize: Optional[Callable[[int], None]] = None
        self._on_move_end: Optional[Callable[[int, int], None]] = None
        self._on_dismissed: Optional[Callable[[], None]] = None
        self._current_w = 0
        self._current_h = 0
        self._resize_start: "tuple[int, int] | None" = None  # (mouse_x_root, width_at_press)
        self._move_start: "tuple[int, int, int, int] | None" = None  # (mouse_x_root, mouse_y_root, win_x_at_press, win_y_at_press)
        self._dragged = False
        self._grip_corner = "top-right"  # shapes.draw_resize_grip과 반드시 같은 값을 써야 함

    def set_on_click(self, callback: "Callable[[], None] | None") -> None:
        """클릭하면 호출할 콜백 등록 — 호출부가 "클릭해서 닫기" 등을 구현할 때 사용.
        ensure()가 창을 새로 만들 때마다 바인딩하므로 창 재생성 전에 먼저 호출해도 된다."""
        self._on_click = callback

    def set_on_resize(self, callback: "Callable[[int], None] | None") -> None:
        """우상단 grip을 드래그하는 동안 새 폭(px)을 계속 전달받을 콜백 등록.
        실제 최소/최대 폭 clamp는 호출부 책임(여기선 원본 델타만 넘김)."""
        self._on_resize = callback

    def set_on_move_end(self, callback: "Callable[[int, int], None] | None") -> None:
        """grip이 아닌 본문을 드래그해서 풍선 전체를 옮겼을 때, 드래그가 끝나는 순간
        (dx, dy) 총 이동량을 전달받을 콜백 등록 — 호출부가 이 값을 누적 저장해서
        다음 렌더부터 "앵커 위치 + 사용자가 옮긴 만큼"으로 유지하는 데 쓴다.
        드래그 중 창 이동 자체는 이 클래스가 즉시 처리하므로 콜백은 끝난 뒤 1번만 온다."""
        self._on_move_end = callback

    def set_on_dismissed(self, callback: "Callable[[], None] | None") -> None:
        """페이드아웃(schedule_dismiss)이 끝까지 진행돼 창이 사라진 순간 1회 호출 —
        호출부가 "이 풍선은 이제 사라졌다"고 상태를 정리(재등장 방지)하는 데 쓴다.
        수동 hide()/clear로 숨길 때는 부르지 않는다(자동 페이드 완료 경로 전용)."""
        self._on_dismissed = callback

    def set_grip_corner(self, corner: str) -> None:
        """"top-right" 또는 "top-left" — draw_*(..., grip_corner=...)로 그린 것과 맞춰야
        클릭 판정이 실제 그려진 손잡이 위치와 일치한다. place() 호출 전에 설정할 것."""
        self._grip_corner = corner

    def ensure(self) -> tk.Canvas:
        if self.win is None or not self.win.winfo_exists():
            self.win = tk.Toplevel(self._root)
            self.win.overrideredirect(True)
            # 영구 topmost는 안 준다 — 캐릭터(character.py)만 항상 위에 있으면 되고, 이
            # 풍선은 place()마다 lift()로 "그 순간" 앞으로 나오는 것으로 충분하다.
            # 영구 topmost면 다른 앱(예: VS Code) 위에서 작업 중에도 계속 덮고 있어서
            # 불필요하게 방해된다.
            self.win.attributes("-transparentcolor", _CHROMA)
            self.win.configure(bg=_CHROMA)
            self.canvas = tk.Canvas(self.win, bg=_CHROMA, highlightthickness=0, bd=0)
            self.canvas.pack()
            self.canvas.bind("<Enter>", lambda e: self._set_hover(True))
            self.canvas.bind("<Leave>", lambda e: self._set_hover(False))
            self.canvas.bind("<ButtonPress-1>", self._handle_press)
            self.canvas.bind("<B1-Motion>", self._handle_drag)
            self.canvas.bind("<ButtonRelease-1>", self._handle_release)
        return self.canvas

    def is_moving(self) -> bool:
        """지금 사용자가 본문을 드래그해서 창을 이동 중인지(리사이즈는 별개) — 호출부가
        콘텐츠 갱신으로 인한 재배치가 이동 드래그와 충돌하지 않게 방어할 때 쓴다.
        그렇지 않으면 새 텍스트가 도착해 place()가 호출될 때마다 드래그 중인 창이
        자동 위치로 튕겨 돌아가서, release 시점에 잘못된 델타가 계산되어 저장 위치
        자체가 틀어진다.

        리사이즈(_resize_start)는 여기 포함하지 않는다 — 리사이즈는 앵커(꼬리) 쪽을
        고정하고 반대쪽 위치를 정상적으로 재계산해야 하므로, 이동과 같은 취급을 하면
        "항상 좌상단이 고정되고 우하단으로만 커지는" 버그가 된다."""
        return self._move_start is not None

    def _in_grip_zone(self, x: int, y: int) -> bool:
        """shapes.draw_resize_grip이 실제로 그린(불투명한 몸통 모서리 안쪽, TAIL_REACH만큼
        캔버스 가장자리에서 들어온) 자리와 히트존을 맞춘다 — margin 없이 캔버스 맨 끝(0)
        기준으로 판정하면 그 영역이 크로마키 투명 여백이라 클릭이 창을 그냥 통과해버려서
        (뒷배경 클릭으로 처리됨) 리사이즈 손잡이를 눌러도 반응하지 않는 버그가 된다."""
        if self._on_resize is None:
            return False
        if self._grip_corner == "top-left":
            return TAIL_REACH <= x <= TAIL_REACH + GRIP_SIZE and TAIL_REACH <= y <= TAIL_REACH + GRIP_SIZE
        return (
            self._current_w - TAIL_REACH - GRIP_SIZE <= x <= self._current_w - TAIL_REACH
            and TAIL_REACH <= y <= TAIL_REACH + GRIP_SIZE
        )

    def _handle_press(self, event) -> None:
        if self._in_grip_zone(event.x, event.y):
            self._resize_start = (event.x_root, self._current_w)
            return
        if self.win is not None:
            self._move_start = (event.x_root, event.y_root, self.win.winfo_x(), self.win.winfo_y())
            self._dragged = False

    def _handle_drag(self, event) -> None:
        if self._resize_start is not None:
            if self._on_resize is not None:
                start_x_root, start_w = self._resize_start
                delta = event.x_root - start_x_root
                if self._grip_corner == "top-left":
                    delta = -delta  # 왼쪽 손잡이는 왼쪽으로 끌수록 넓어져야 함
                self._on_resize(start_w + delta)
            return
        if self._move_start is None or self.win is None:
            return
        start_x_root, start_y_root, win_x, win_y = self._move_start
        dx = event.x_root - start_x_root
        dy = event.y_root - start_y_root
        if not self._dragged and abs(dx) + abs(dy) < 3:
            return  # 클릭인지 드래그인지 구분하는 여유(작은 흔들림은 클릭으로 취급)
        self._dragged = True
        self.win.geometry(f"+{win_x + dx}+{win_y + dy}")

    def _handle_release(self, _event=None) -> None:
        if self._resize_start is not None:
            self._resize_start = None
            return
        if self._move_start is not None:
            start_x_root, start_y_root, win_x, win_y = self._move_start
            was_dragged = self._dragged
            self._move_start = None
            self._dragged = False
            if was_dragged:
                if self._on_move_end is not None and self.win is not None:
                    self._on_move_end(self.win.winfo_x() - win_x, self.win.winfo_y() - win_y)
            else:
                self._handle_click()

    def _handle_click(self) -> None:
        if self._on_click is not None:
            self._on_click()

    def place(self, x: int, y: int, w: int, h: int) -> None:
        if self.win is None:
            return
        self._current_w, self._current_h = w, h
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()

    def _set_hover(self, hovering: bool) -> None:
        self._hover = hovering
        if not hovering and self._pending_dwell_ms is not None:
            self._schedule_dismiss(self._pending_dwell_ms)

    def schedule_dismiss(self, dwell_ms: int) -> None:
        """dwell_ms 뒤에 fade-out 시작. 스트리밍 중처럼 계속 미루고 싶으면 매번 다시 호출."""
        self._pending_dwell_ms = dwell_ms
        if self._hover:
            return
        self._schedule_dismiss(dwell_ms)

    def _schedule_dismiss(self, dwell_ms: int) -> None:
        self.cancel_dismiss()
        self._dismiss_after_id = self._root.after(dwell_ms, self._start_fade)

    def cancel_dismiss(self) -> None:
        if self._dismiss_after_id is not None:
            try:
                self._root.after_cancel(self._dismiss_after_id)
            except Exception:
                pass
            self._dismiss_after_id = None

    def _start_fade(self, steps: int = 8, step_ms: int = 25) -> None:
        self._cancel_fade()
        self._fade_step(steps, steps, step_ms)

    def _fade_step(self, remaining: int, total: int, step_ms: int) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        if remaining <= 0:
            self.hide()
            if self._on_dismissed is not None:
                try:
                    self._on_dismissed()
                except Exception:
                    pass
            return
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        factor = 0.85
        try:
            self.canvas.scale("all", w / 2, h / 2, factor, factor)
        except Exception:
            pass
        new_w = max(4, int(w * factor))
        new_h = max(4, int(h * factor))
        x = self.win.winfo_x() + (w - new_w) // 2
        y = self.win.winfo_y() + (h - new_h) // 2
        self.win.geometry(f"{new_w}x{new_h}+{x}+{y}")
        self._fade_after_id = self._root.after(step_ms, lambda: self._fade_step(remaining - 1, total, step_ms))

    def _cancel_fade(self) -> None:
        if self._fade_after_id is not None:
            try:
                self._root.after_cancel(self._fade_after_id)
            except Exception:
                pass
            self._fade_after_id = None

    def hide(self) -> None:
        self.cancel_dismiss()
        self._cancel_fade()
        if self.win is not None:
            try:
                self.win.withdraw()
            except Exception:
                pass

    def destroy(self) -> None:
        self.hide()
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self.canvas = None
