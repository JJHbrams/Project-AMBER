"""히스토리 패널 — 기존 /stm/messages HTTP 엔드포인트로 과거 대화를 스크롤 리스트로 보여준다.

투명 chroma-key가 아니라 일반 불투명 Toplevel(settings_window.py와 같은 방식) — 히스토리는
찬찬히 읽는 용도라 가독성이 우선이다.
"""

import json
import tkinter as tk
import tkinter.ttk as ttk
import urllib.parse
import urllib.request
from typing import Callable, Optional


class HistoryPanel:
    def __init__(
        self,
        root: tk.Tk,
        get_stm_port: Callable[[], Optional[int]],
        scope_key: str = "overlay",
        cfg_bubble: Optional[dict] = None,
    ):
        self._root = root
        self._get_stm_port = get_stm_port
        self._scope_key = scope_key
        self._cfg = cfg_bubble or {}
        self._win: Optional[tk.Toplevel] = None
        self._list_frame: Optional[ttk.Frame] = None

    def show(self) -> None:
        if self._win is not None and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return

        self._win = tk.Toplevel(self._root)
        self._win.title("대화 기록")
        self._win.geometry("480x560")

        top = ttk.Frame(self._win)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="대화 기록", font=("", 10, "bold")).pack(side="left")
        ttk.Button(top, text="닫기", command=self._win.destroy).pack(side="right")
        ttk.Button(top, text="새로고침", command=self._reload).pack(side="right", padx=(0, 6))

        container = ttk.Frame(self._win)
        container.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self._list_frame = ttk.Frame(canvas)

        self._list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._reload()

    def _reload(self) -> None:
        if self._list_frame is None:
            return
        for child in self._list_frame.winfo_children():
            child.destroy()

        messages = self._fetch_messages()
        if not messages:
            ttk.Label(
                self._list_frame,
                text="기록을 불러올 수 없거나 아직 대화가 없습니다.",
                foreground="gray",
            ).pack(anchor="w", padx=8, pady=8)
            return

        for msg in messages:
            self._render_message(msg)

    def _render_message(self, msg: dict) -> None:
        role = msg.get("role", "")
        content = str(msg.get("content", ""))

        if role == "user":
            anchor, bg, italic = "e", "#dbeeff", False
        elif role == "assistant":
            anchor, bg, italic = "w", "#f2f2f2", False
        else:
            anchor, bg, italic = "w", "#eeeeee", True

        row = ttk.Frame(self._list_frame)
        row.pack(fill="x", padx=8, pady=4, anchor=anchor)

        card = tk.Label(
            row,
            text=content,
            bg=bg,
            wraplength=380,
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
        )
        if italic:
            card.configure(font=("", 9, "italic"), fg="#666666")
        card.pack(side="right" if anchor == "e" else "left")

    def _fetch_messages(self) -> list:
        port = self._get_stm_port()
        if not port:
            return []
        limit = int(self._cfg.get("history_default_limit", 50))
        within_minutes = int(self._cfg.get("history_default_within_minutes", 120))
        qs = urllib.parse.urlencode({"scope_key": self._scope_key, "limit": limit, "within_minutes": within_minutes})
        url = f"http://127.0.0.1:{port}/stm/messages?{qs}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return list(data.get("messages", []))
        except Exception:
            return []
