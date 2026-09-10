"""Parented transparent session carousel, with host-owned foreground policy."""

import hashlib
import math
import logging
import ctypes
import re
import time
import tkinter as tk
import tkinter.font as tkfont
from overlay.bubble import geometry
from overlay.bubble.bubble_window import _toplevel_hwnd
from PIL import Image, ImageDraw, ImageTk
from overlay.session_selection import SessionSelection
from overlay.session_presentation import SessionPresentation
from overlay.session_shadow import SessionShadow

STATE_LABELS = {
    "idle": "○ 대기",
    "working": "● 생성 중",
    "needs_input": "Ⅱ 승인 대기",
    "ready": "✓ 완료",
    "blocked": "◇ 오류",
    "unknown": "○ 미확정",
}
CHROMA = "#010101"
# Monitor-owned palette. Bubble appearance is not a monitor configuration input.
MONITOR_THEME = {
    "speech_bg": "#f4f0e8",
    "speech_fg": "#203039",
    "speech_outline": "#d6dfdd",
    "tool_outline": "#238b94",
    "echo_outline": "#ad7927",
}


def keep_monitor_visible(win):
    """Persistent topmost surface without activating it or stealing keyboard focus."""
    win.attributes("-topmost", True)
    hwnd = _toplevel_hwnd(win)
    if hwnd:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def keep_popup_visible(win):
    """Passive redraw never activates; a deliberate popup click may accept keys."""
    win.attributes("-topmost", True)
    hwnd = _toplevel_hwnd(win)
    if hwnd:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, (style | 0x80) & ~0x08000000)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def muted_color(fg, bg, amount=0.58):
    channels = [
        round(int(fg[i : i + 2], 16) * amount + int(bg[i : i + 2], 16) * (1 - amount))
        for i in (1, 3, 5)
    ]
    return "#" + "".join(f"{c:02x}" for c in channels)


def contrast_ratio(foreground, background):
    def lum(color):
        rgb = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        return sum(
            (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) * w
            for c, w in zip(rgb, (0.2126, 0.7152, 0.0722))
        )

    a, b = sorted((lum(foreground), lum(background)))
    return (b + 0.05) / (a + 0.05)


def safe_identity(row):
    return hashlib.sha256(str(row.get("key", "")).encode()).hexdigest()[:6]


def safe_label(row, ordinal=1):
    label = row.get("label")
    if (
        isinstance(label, str)
        and label.strip()
        and len(label) <= 128
        and not re.search(r"[\\/:\x00-\x1f\x7f]", label)
    ):
        return label.strip()
    return "새 세션"


def ordered_rows(rows):
    return sorted((dict(r) for r in rows), key=lambda r: r.get("first_seen", 0))


def adjacent_position(anchor, size, work):
    x, y, w, h = anchor
    width, height = size
    left, top, right, bottom = work
    py = y - height - 12
    if py < top:
        py = y + h + 12
    return max(left, min(x + w // 2 - width // 2, right - width)), max(
        top, min(py, bottom - height)
    )


class SessionStackWidget:
    def __init__(
        self, parent, *, theme=None, selection=None, on_change=None, on_rename=None
    ):
        if parent is None:
            raise ValueError("Explicit parent required")
        self.parent = parent
        self.theme = dict(MONITOR_THEME)
        self.bg, self.fg = self.theme["speech_bg"], self.theme["speech_fg"]
        if contrast_ratio(self.fg, self.bg) < 7:
            self.fg = max(
                ("#000000", "#ffffff"), key=lambda c: contrast_ratio(c, self.bg)
            )
        self.selection = selection or SessionSelection()
        self.presentation = SessionPresentation()
        self.on_change = on_change or (lambda: None)
        self.on_rename = on_rename
        self._editor = self._editing_key = None
        self.surfaces, self.canvases = [], []
        self._card_images = {}
        self._card_masks = {}
        self._graphic_images = {}
        for alpha in (0.65, 0.65, 1.0, 0.94):
            win = tk.Toplevel(parent)
            win.withdraw()
            win.overrideredirect(True)
            win.configure(bg=CHROMA)
            try:
                win.attributes("-transparentcolor", CHROMA)
                win.attributes("-alpha", alpha)
            except tk.TclError:
                pass
            canvas = tk.Canvas(win, bg=CHROMA, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<MouseWheel>", self._wheel)
            keep_monitor_visible(win)
            self.surfaces.append(win)
            self.canvases.append(canvas)
        self.win, self.canvas = self.surfaces[2], self.canvases[2]
        self.shadow = None
        self._shadow_failures = 0
        self._shadow_retry_at = 0
        self._shadow_warned = False
        self.font = tkfont.Font(self.win, family="Malgun Gothic", size=10)
        self.small_font = tkfont.Font(self.win, family="Malgun Gothic", size=8)
        self.heading_font = tkfont.Font(
            self.win, family="Malgun Gothic", size=14, weight="bold"
        )
        self.popup_heading_font = tkfont.Font(
            self.win, family="Malgun Gothic", size=12, weight="bold"
        )
        self._foreground, self._topmost, self._below_hwnd = False, False, 0
        self._anchor = self._work = self._settle_after = None
        self.rows, self.visible_rows, self.bounds = [], [], []
        self._visible = False
        self._animation_after = None
        self._mode_after = None
        self._mode_phase = 0
        self._animation = None
        self._display_key = None
        self._roll_direction = 1
        self.popup = self.popup_canvas = None
        self._popup_page = 0
        self.popup_rows = []
        self._render_signature = self._popup_signature = None
        self._stack_order = None
        self._tooltip = None
        self._tooltip_after = None
        self._title_preview = None
        self._buttons = {}
        self._pressed_button = None
        self._navigation_menu = tk.Menu(parent, tearoff=False)
        self._navigation_menu.add_command(
            label="이전 세션", command=lambda: self.browse(-1)
        )
        self._navigation_menu.add_command(
            label="다음 세션", command=lambda: self.browse(1)
        )

    def _content_signature(self):
        return tuple(
            (
                row["key"],
                row.get("provider"),
                safe_label(row),
                row.get("state"),
                self.presentation.state(row),
                row.get("subagent_count"),
                repr(row.get('child_activity')), row.get('activity_category'),
                row.get("acknowledged"),
                row.get("project_name"),
                row.get("agent_name"),
            )
            for row in self.rows
        )

    def _sync_shadow(self, force=False):
        if (
            not self._visible
            or self._shadow_failures >= 3
            or time.monotonic() < self._shadow_retry_at
        ):
            return
        try:
            if self.shadow is None:
                self.shadow = SessionShadow()
            self.shadow.update(
                self.win._native_bounds,
                self._work,
                float(self.win.attributes("-alpha")),
            )
            self.shadow.place_behind(_toplevel_hwnd(self.win), force=force)
        except Exception:
            # Decoration cannot stop state refresh. Retry at most three times
            # on existing host refreshes, not on a new shadow timer.
            self._shadow_failures += 1
            self._shadow_retry_at = time.monotonic() + 0.25
            self._hide_shadow()
            if not self._shadow_warned:
                logging.getLogger(__name__).warning(
                    "Session shadow unavailable; monitoring remains active"
                )
                self._shadow_warned = True
        else:
            self._shadow_failures = 0

    def _hide_shadow(self):
        if self.shadow is not None:
            try:
                self.shadow.hide()
            except Exception:
                pass

    def _queue_tooltip(self, event, text):
        self._hide_tooltip()
        x, y = event.x_root, event.y_root

        def show():
            self._tooltip_after = None
            if not self._visible:
                return
            if self._tooltip is None:
                self._tooltip = tk.Toplevel(self.parent)
                self._tooltip.withdraw()
                self._tooltip.overrideredirect(True)
                self._tooltip.configure(bg=self.bg)
                self._tooltip_label = tk.Label(
                    self._tooltip,
                    bg=self.bg,
                    fg=self.fg,
                    font=self.small_font,
                    wraplength=240,
                    padx=8,
                    pady=5,
                )
                self._tooltip_label.pack()
            self._tooltip_label.configure(text=text)
            self._tooltip.update_idletasks()
            width = min(self._tooltip.winfo_reqwidth(), self._work[2] - self._work[0])
            height = self._tooltip.winfo_reqheight()
            px, py = geometry.clamp_rect(x, y + 18, width, height, self._work)
            self._tooltip.geometry(f"{width}x{height}+{px}+{py}")
            keep_monitor_visible(self._tooltip)
            self._tooltip.deiconify()

        self._tooltip_after = self.parent.after(350, show)

    def _hide_tooltip(self):
        self._title_preview = None
        if self._tooltip_after is not None:
            self.parent.after_cancel(self._tooltip_after)
            self._tooltip_after = None
        if self._tooltip is not None:
            self._tooltip.withdraw()

    def _title_enter(self, event, key, text):
        self._queue_tooltip(event, text)
        self._title_preview = (key, text)

    def _button_visual(self, tag, state):
        button = self._buttons.get(tag)
        if button is None:
            return
        canvas = self.canvases[3]
        if not button["enabled"]:
            fill, edge, fg = "#e9eeec", "#d5dcda", "#82908e"
        else:
            fill = {"hover": "#cfe6df", "pressed": "#b2d9cd"}.get(state, "#e0eeeb")
            edge, fg = "#93b5ac", self.fg
        canvas.itemconfigure(button["shape"], fill=fill, outline=edge)
        canvas.itemconfigure(button["text"], fill=fg)
        button["photo"] = self._button_image(tag, button, state)
        canvas.itemconfigure(button["image"], image=button["photo"])
        button["visual"] = state

    def _button_enter(self, event, tag, tip):
        button = self._buttons[tag]
        self.canvases[3].configure(cursor="hand2" if button["enabled"] else "arrow")
        self._button_visual(tag, "hover")
        self._queue_tooltip(event, tip)

    def _button_leave(self, tag):
        if self._pressed_button == tag:
            self._pressed_button = None
        self.canvases[3].configure(cursor="arrow")
        self._button_visual(tag, "normal")
        self._hide_tooltip()

    def _button_press(self, tag):
        self._hide_tooltip()
        if self._buttons[tag]["enabled"]:
            self._pressed_button = tag
            self._button_visual(tag, "pressed")

    def _button_release(self, event, tag):
        button = self._buttons.get(tag)
        pressed = self._pressed_button
        self._pressed_button = None
        if button is None:
            return
        x1, y1, x2, y2 = button["bounds"]
        inside = x1 <= event.x <= x2 and y1 <= event.y <= y2
        self._button_visual(tag, "hover" if inside else "normal")
        if pressed == tag and inside and button["enabled"]:
            button["callback"]()

    def update_rows(self, rows):
        previous_keys = {row["key"] for row in self.rows}
        self.selection.update(rows)
        self.rows = self.selection.rows
        self.presentation.update(self.rows, self.selection.selected_key)
        if self._editing_key is not None and self._editing_key not in {
            r["key"] for r in self.rows
        }:
            self._close_editor()
        if previous_keys != {row["key"] for row in self.rows}:
            self._cancel_animation()
            self._display_key = None
        if not self.rows:
            self.hide()
        elif self._visible:
            self._render()
        self._render_popup()

    def update_theme(self, theme=None):
        """Compatibility redraw; legacy bubble-theme arguments cannot recolor us."""
        self.theme = dict(MONITOR_THEME)
        self.bg, self.fg = self.theme["speech_bg"], self.theme["speech_fg"]
        if contrast_ratio(self.fg, self.bg) < 7:
            self.fg = max(
                ("#000000", "#ffffff"), key=lambda c: contrast_ratio(c, self.bg)
            )
        self._render()
        self._render_popup()

    def _wheel(self, event):
        if event.delta:
            notches = max(1, abs(int(event.delta)) // 120)
            self.browse(-notches if event.delta > 0 else notches)
        return "break"

    def browse(self, delta):
        self._hide_tooltip()
        self._roll_direction = 1 if delta > 0 else -1
        self.selection.browse(delta)
        if self._settle_after is not None:
            self.parent.after_cancel(self._settle_after)
        self._settle_after = self.parent.after(400, self._settle)
        self._render()

    def _settle(self):
        self._settle_after = None
        self.selection.settle()
        if self.selection.deadline is not None:
            self._settle_after = self.parent.after(10, self._settle)
        self._render()
        self.on_change()

    def resume_auto(self):
        self._hide_tooltip()
        self.selection.resume_auto()
        self._render()
        self.on_change()

    def toggle_auto(self):
        self._hide_tooltip()
        self.selection.set_auto_enabled(not self.selection.auto_enabled)
        self._render()
        self.on_change()
        message = (
            "자동 ON: 작업 완료 후 새 세션으로 이동합니다."
            if self.selection.auto_enabled
            else "자동 전환 OFF · 선택한 세션 유지, 휠로 직접 이동"
        )
        from types import SimpleNamespace

        self._queue_tooltip(
            SimpleNamespace(x_root=self.bounds[3][0], y_root=self.bounds[3][1]), message
        )

    def _mode_frame(self):
        """Move one short edge item; never repaint content or touch native order."""
        self._mode_after = None
        if not self._visible or not self.selection.auto_enabled:
            return
        self._mode_phase += 2.5
        self.canvas.coords("mode_edge", *self._edge_points(self._mode_phase))
        self._mode_after = self.parent.after(50, self._mode_frame)

    def _edge_points(self, phase):
        """A continuous short segment inside the shell's rounded perimeter."""
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        radius = 12
        left, top, right, bottom = 2, 2, width - 3, height - 3
        points = []
        for cx, cy, start in (
            (right - radius, top + radius, -90),
            (right - radius, bottom - radius, 0),
            (left + radius, bottom - radius, 90),
            (left + radius, top + radius, 180),
        ):
            for step in range(9):
                angle = math.radians(start + step * 90 / 8)
                points.append(
                    (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
                )
        points.append(points[0])
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        perimeter = sum(lengths)
        result = []
        for distance in range(0, 33, 4):
            position = (phase + distance) % perimeter
            for a, b, length in zip(points, points[1:], lengths):
                if position <= length:
                    fraction = position / max(length, 0.001)
                    result.extend(
                        (
                            a[0] + (b[0] - a[0]) * fraction,
                            a[1] + (b[1] - a[1]) * fraction,
                        )
                    )
                    break
                position -= length
        return result

    def _sync_mode_effect(self):
        if self._mode_after is not None and not self.selection.auto_enabled:
            self.parent.after_cancel(self._mode_after)
            self._mode_after = None
        if self.selection.auto_enabled and self._visible and self._mode_after is None:
            self._mode_after = self.parent.after(50, self._mode_frame)

    def jump_attention(self, state=None):
        self._hide_tooltip()
        self.selection.jump_attention(state)
        self._render()
        self.on_change()

    def _fit(self, text, width, font=None):
        font = font or self.font
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def _footer_text(self, row, width):
        from overlay.state_api import valid_project_name

        identity = safe_identity(row)
        provider = {
            "claude": "Claude",
            "codex": "Codex",
            "copilot": "Copilot",
            "antigravity": "Antigravity",
            "mcp": "MCP",
        }.get(row.get("provider"), "MCP")
        from overlay.state_api import ALLOWED_AGENT_NAMES
        agent_name = row.get("agent_name")
        if isinstance(agent_name, str) and agent_name in ALLOWED_AGENT_NAMES:
            provider = agent_name
        suffix = " · " + identity
        prefix = provider + " · "
        project = row.get("project_name")
        if valid_project_name(project):
            remaining = width - self.small_font.measure(prefix + suffix)
            name = self._fit(project.strip(), remaining, self.small_font)
            if name:
                return prefix + name + suffix
        # Project is removed first. Even on constrained secondary surfaces,
        # keep the identity intact before shortening the truthful provider label.
        remaining = width - self.small_font.measure(suffix)
        provider = self._fit(provider, remaining, self.small_font)
        return provider + suffix if provider else self._fit(identity, width, self.small_font)

    def _card_background(self, canvas, width, height, center=False, rear=False):
        """Supersampled code-native shell; chromakey stays exact outside the card."""
        edge = (
            ("#358e90" if self.selection.auto_enabled else "#708399")
            if center
            else self.theme["speech_outline"]
        )
        key = (width, height, self.bg, edge, center, rear)
        if key not in self._card_images:
            scale = 4
            bitmap = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(bitmap)
            # Antialias against the card edge, not a private desktop screenshot.
            draw.rounded_rectangle(
                (4, 4, (width - 1) * scale, (height - 1) * scale),
                radius=14 * scale,
                fill="#353a3c" if rear else self.bg,
                outline=edge,
                width=scale,
            )
            # Quiet code-native bevel/gradient, not acrylic or desktop blur.
            inset = Image.new("L", bitmap.size, 0)
            ImageDraw.Draw(inset).rounded_rectangle(
                (2 * scale, 2 * scale, (width - 2) * scale, (height - 2) * scale),
                radius=13 * scale,
                fill=255,
            )
            gradient = Image.new("RGBA", bitmap.size)
            paint = ImageDraw.Draw(gradient)
            top, bottom = (
                ((59, 63, 65), (40, 44, 46))
                if rear
                else ((250, 247, 240), (240, 235, 224))
            )
            for line in range(height * scale):
                fraction = line / max(1, height * scale - 1)
                color = tuple(
                    round(a + (b - a) * fraction) for a, b in zip(top, bottom)
                )
                paint.line((0, line, width * scale, line), fill=(*color, 255))
            bitmap.paste(gradient, (0, 0), inset)
            draw = ImageDraw.Draw(bitmap)
            # Boundaries are drawn last, never buried by the gradient inset.
            draw.rounded_rectangle(
                (4, 4, (width - 1) * scale, (height - 1) * scale),
                radius=14 * scale,
                outline="#62686a" if rear else edge,
                width=scale,
            )
            if center:
                draw.rounded_rectangle(
                    (3 * scale, 3 * scale, (width - 3) * scale, (height - 3) * scale),
                    radius=12 * scale,
                    outline="#acd0c9" if self.selection.auto_enabled else "#bbc5ce",
                    width=scale,
                )
            bitmap = bitmap.resize((width, height), Image.Resampling.LANCZOS)
            # Tk chromakey cannot express per-pixel alpha. Keep exterior pixels
            # exactly keyed; blending against the key would leave black halos.
            pixels = [
                (r, g, b) if a >= 128 else (1, 1, 1) for r, g, b, a in bitmap.getdata()
            ]
            bitmap = Image.new("RGB", (width, height))
            bitmap.putdata(pixels)
            if len(self._card_images) > 24:
                self._card_images.clear()
                self._card_masks.clear()
            self._card_images[key] = ImageTk.PhotoImage(bitmap, master=self.parent)
            mask = Image.new("L", (width, height))
            mask.putdata([0 if pixel == (1, 1, 1) else 255 for pixel in pixels])
            self._card_masks[key] = mask
        canvas._shell_mask = self._card_masks[key]
        # Visible canvases, not the bounded resize cache, own their live image.
        canvas._shell_image = self._card_images[key]
        canvas.create_image(0, 0, anchor="nw", image=canvas._shell_image)

    @staticmethod
    def _draw_graphic(draw, name, box, color, scale=4):
        x, y, w, h = box

        def line(points):
            draw.line(
                [
                    (round((x + a * w) * scale), round((y + b * h) * scale))
                    for a, b in points
                ],
                fill=color,
                width=round(1.2 * scale),
                joint="curve",
            )

        rect = (
            round(x * scale),
            round(y * scale),
            round((x + w) * scale),
            round((y + h) * scale),
        )
        if name == "dot":
            draw.ellipse(rect, fill=color)
        elif name == "working":
            for angle in range(0, 360, 60):
                draw.arc(rect, angle, angle + 36, fill=color, width=round(1.2 * scale))
        elif name in ("waiting", "needs_input", "ready"):
            draw.ellipse(rect, outline=color, width=round(1.2 * scale))
            if name == "needs_input":
                line(((0.35, 0.25), (0.35, 0.75)))
                line(((0.65, 0.25), (0.65, 0.75)))
            else:
                line(((0.23, 0.52), (0.43, 0.72), (0.78, 0.3)))
        elif name == "unknown":
            draw.ellipse(rect, outline=color, width=round(1.2 * scale))
            line(
                (
                    (0.3, 0.3),
                    (0.42, 0.2),
                    (0.63, 0.24),
                    (0.7, 0.38),
                    (0.5, 0.55),
                    (0.5, 0.63),
                )
            )
            line(((0.5, 0.76), (0.5, 0.8)))
        elif name == "idle":
            draw.ellipse(rect, outline=color, width=round(1.2 * scale))
        elif name in ("blocked",):
            draw.ellipse(rect, outline=color, width=round(1.2 * scale))
            line(((0.5, 0.23), (0.5, 0.58)))
            line(((0.5, 0.73), (0.5, 0.77)))
        elif name == "list":
            for position in (0.2, 0.5, 0.8):
                line(((0, position), (0.13, position)))
                line(((0.33, position), (1, position)))
        elif name == "pin":
            draw.polygon(
                [
                    (round((x + a * w) * scale), round((y + b * h) * scale))
                    for a, b in (
                        (0.25, 0),
                        (0.75, 0),
                        (0.67, 0.48),
                        (0.9, 0.65),
                        (0.1, 0.65),
                        (0.33, 0.48),
                    )
                ],
                fill=color,
            )
            line(((0.5, 0.62), (0.5, 1)))

    def _add_graphic(self, canvas, name, x, y, size, color, tag):
        key = ("icon", name, size, color)
        if key not in self._graphic_images:
            if len(self._graphic_images) >= 128:
                self._graphic_images.clear()
            image = Image.new("RGBA", (size * 4, size * 4))
            self._draw_graphic(
                ImageDraw.Draw(image), name, (2, 2, size - 4, size - 4), color
            )
            self._graphic_images[key] = ImageTk.PhotoImage(
                image.resize((size, size), Image.Resampling.LANCZOS), master=self.parent
            )
        image = self._graphic_images[key]
        canvas._graphics.append(image)
        canvas.create_image(x, y, image=image, tags=tag)

    def _add_mode_chip(self, canvas, right, y, text, color):
        width, height = self.small_font.measure(text) + 12, 18
        key = ("chip", width, color)
        if key not in self._graphic_images:
            bitmap = Image.new("RGBA", (width * 4, height * 4))
            ImageDraw.Draw(bitmap).rounded_rectangle(
                (2, 2, width * 4 - 3, height * 4 - 3),
                radius=4 * 4,
                fill="#f8f5ef",
                outline=color,
                width=3,
            )
            self._graphic_images[key] = ImageTk.PhotoImage(
                bitmap.resize((width, height), Image.Resampling.LANCZOS),
                master=self.parent,
            )
        image = self._graphic_images[key]
        canvas._graphics.append(image)
        canvas.create_image(right + 6, y, image=image, anchor="e", tags="mode_chip")

    def _button_image(self, tag, button, state):
        x1, y1, x2, y2 = button["bounds"]
        width, height = round(x2 - x1), round(y2 - y1)
        enabled = button["enabled"]
        key = (
            "button",
            tag,
            width,
            height,
            state,
            enabled,
            self.selection.auto_enabled,
            button.get("emphasis", False),
        )
        if key not in self._graphic_images:
            if len(self._graphic_images) >= 128:
                self._graphic_images.clear()
            image = Image.new("RGBA", (width * 4, height * 4))
            draw = ImageDraw.Draw(image)
            fill = (
                "#e9e6df"
                if not enabled
                else {"hover": "#e4efeb", "pressed": "#c8dfd7"}.get(state, "#f8f5ee")
            )
            edge = "#c5c3bd" if not button.get("emphasis") else "#358e90"
            draw.rounded_rectangle(
                (2, 2, width * 4 - 3, height * 4 - 3),
                radius=5 * 4,
                fill=fill,
                outline=edge,
                width=4,
            )
            color = self.fg if enabled else "#858984"
            if tag == "auto":
                left, top = width - 33, (height - 14) / 2
                on = self.selection.auto_enabled
                draw.rounded_rectangle(
                    (left * 4, top * 4, (left + 26) * 4, (top + 14) * 4),
                    radius=7 * 4,
                    fill="#267f82" if on else "#8c969a",
                )
                thumb_x = left + 19 if on else left + 7
                draw.ellipse(
                    (
                        (thumb_x - 5.5) * 4,
                        (top + 1.5) * 4,
                        (thumb_x + 5.5) * 4,
                        (top + 12.5) * 4,
                    ),
                    fill="#ffffff",
                )
            else:
                self._draw_graphic(draw, tag, (7, (height - 11) / 2, 11, 11), color)
            self._graphic_images[key] = ImageTk.PhotoImage(
                image.resize((width, height), Image.Resampling.LANCZOS),
                master=self.parent,
            )
        return self._graphic_images[key]

    def _cancel_animation(self):
        if self._animation_after is not None:
            self.parent.after_cancel(self._animation_after)
            self._animation_after = None
        self._animation = None

    def _start_animation(self, shown, bounds):
        self._cancel_animation()
        cards = list(zip(self.surfaces[:3], self.canvases[:3]))
        available = list(cards)
        assigned = []
        plans = {}
        # >=3 rows: retain every common card key's HWND, permuting physical
        # surfaces between upper/lower/center roles. Two rows use a symmetric
        # fade/slide fallback. Only the lower rear is visible with two rows.
        for role, row in enumerate(shown):
            match = next(
                (
                    card
                    for card in available
                    if getattr(card[0], "_card_key", None) == row["key"]
                ),
                None,
            )
            if match is not None and len(self.rows) >= 3:
                assigned.append(match)
                available.remove(match)
            else:
                assigned.append(None)
        for role in range(3):
            if assigned[role] is None:
                assigned[role] = available.pop(0)
        for role, (win, canvas) in enumerate(assigned):
            old_role = self.surfaces.index(win)
            old_row = getattr(win, "_card_row", shown[role])
            plans[win] = {
                "from": (
                    win.winfo_x(),
                    win.winfo_y(),
                    win.winfo_width(),
                    win.winfo_height(),
                ),
                "alpha": float(win.attributes("-alpha")),
                "to": bounds[role],
                "old": old_row,
                "new": shown[role],
                "recycle": len(self.rows) == 2
                or old_row["key"] != shown[role]["key"]
                or (old_role in (0, 1) and role in (0, 1) and old_role != role),
                "switched": False,
            }
        self.surfaces[:3] = [pair[0] for pair in assigned]
        self.canvases[:3] = [pair[1] for pair in assigned]
        self.win, self.canvas = self.surfaces[2], self.canvases[2]
        self._animation = {"started": time.monotonic(), "plans": plans}

    def _animation_frame(self):
        self._animation_after = None
        self._render()

    def _animated_card(self, win, role, row, bounds, progress):
        plan = self._animation["plans"][win]
        target_alpha = 1.0 if role == 2 else 0.65
        start, end = plan["from"], bounds
        portion = progress
        alpha = plan["alpha"] + (target_alpha - plan["alpha"]) * progress
        if plan["recycle"]:
            if progress < 0.5:
                row = plan["old"]
                end = (
                    start[0],
                    start[1] - 12 * self._roll_direction,
                    start[2],
                    start[3],
                )
                portion = progress * 2
                alpha = plan["alpha"] * (1 - portion)
            else:
                if not plan["switched"]:
                    win.attributes("-alpha", 0)
                    win.update_idletasks()
                    plan["switched"] = True
                start = (end[0], end[1] + 12 * self._roll_direction, end[2], end[3])
                portion = (progress - 0.5) * 2
                alpha = target_alpha * portion
        eased = portion * portion * (3 - 2 * portion)
        pose = tuple(round(a + (b - a) * eased) for a, b in zip(start, end))
        px, py = geometry.clamp_rect(*pose, self._work)
        return row, (px, py, pose[2], pose[3]), alpha

    def _state_icon(self, canvas, state, x, y, color):
        if state == "working":
            for angle in (10, 130, 250):
                canvas.create_arc(
                    x - 11,
                    y - 11,
                    x + 11,
                    y + 11,
                    start=angle,
                    extent=94,
                    style="arc",
                    outline=color,
                    width=2,
                )
        elif state == "needs_input":
            canvas.create_oval(x - 11, y - 11, x + 11, y + 11, outline=color, width=2)
            for dx in (-3, 3):
                canvas.create_line(x + dx, y - 5, x + dx, y + 5, fill=color, width=2)
        elif state == "ready":
            canvas.create_line(
                x - 8, y, x - 2, y + 6, x + 9, y - 7, fill=color, width=2
            )
        elif state == "blocked":
            canvas.create_polygon(
                x,
                y - 12,
                x + 12,
                y,
                x,
                y + 12,
                x - 12,
                y,
                fill="",
                outline=color,
                width=2,
            )
            canvas.create_text(x, y, text="!", fill=color, font=self.font)
        else:
            canvas.create_oval(x - 10, y - 10, x + 10, y + 10, outline=color, width=2)

    def _render(self):
        if not self._visible or not self.rows or self._anchor is None:
            return
        signature = (
            self._content_signature(),
            self.selection.selected_key,
            self.selection.candidate_key,
            self.selection.pinned_key,
            self.selection.auto_enabled,
            self._anchor,
            self._work,
            tuple(self.theme.items()),
        )
        if self._animation is None and signature == self._render_signature:
            if self._shadow_failures:
                self._sync_shadow()
            return
        self._render_signature = signature
        work = self._work
        total_w = min(max(220, min(360, self._anchor[2] + 40)), work[2] - work[0])
        card_h = 76 if len(self.rows) == 1 else (104 if len(self.rows) == 2 else 136)
        toolbar_h = 36 if total_w >= 300 else 64
        deck_h = card_h + toolbar_h + 4
        if total_w < 190 or work[3] - work[1] < deck_h:
            self.hide()
            return
        cw = total_w
        self.heading_font.configure(size=12 if cw >= 280 else 11)
        x, y = adjacent_position(self._anchor, (total_w, deck_h), work)
        key = self.selection.candidate_key or self.selection.selected_key
        keys = [r["key"] for r in self.rows]
        index = keys.index(key)
        center = self.rows[index]
        if self._title_preview is not None and self._title_preview != (key, safe_label(center)):
            self._hide_tooltip()
        shown = (
            self.rows[(index - 1) % len(self.rows)],
            self.rows[(index + 1) % len(self.rows)],
            center,
        )
        self.visible_rows = [center]
        center_y = y + (30 if len(self.rows) >= 3 else 0)
        self.bounds = [
            (x + 12, y, cw - 24, 68),
            (x + 12, y + (68 if len(self.rows) >= 3 else 36), cw - 24, 68),
            (x, center_y, cw, 76),
            (x + 8, y + card_h + 4, cw - 16, toolbar_h),
        ]
        if (
            key != self._display_key
            and self._display_key is not None
            and len(self.rows) > 1
        ):
            self._start_animation(shown, self.bounds)
        self._display_key = key
        progress = (
            min(1.0, (time.monotonic() - self._animation["started"]) / 0.18)
            if self._animation
            else 1.0
        )
        for i, (win, c, bounds) in enumerate(
            zip(self.surfaces, self.canvases, self.bounds)
        ):
            row = shown[i] if i < 3 else None
            alpha = (0.65, 0.65, 1.0, 0.94)[i]
            if self._animation and i < 3:
                row, bounds, alpha = self._animated_card(win, i, row, bounds, progress)
            px, py, w, h = bounds
            c.configure(width=w, height=h)
            c.delete("all")
            c._graphics = []
            self._card_background(c, w, h, i == 2, rear=i < 2)
            if getattr(win, "_native_bounds", None) != bounds:
                win.geometry(f"{w}x{h}+{px}+{py}")
                win._native_bounds = bounds
            if i < 3:
                win._card_key, win._card_row = row["key"], dict(row)
                font = self.heading_font if i == 2 else self.small_font
                reserve = 58 if i == 2 else 0
                padding = 16 if i == 2 else 12
                title = self._fit(safe_label(row), w - padding * 2 - reserve, font)
                c.create_text(
                    padding,
                    38 if i == 2 else (12 if i == 0 else h - 24),
                    text=title,
                    fill=self.fg if i == 2 else "#f1f0e9",
                    font=font,
                    anchor="w",
                    tags="title",
                )
                # Full-title preview only where elision actually hides content.
                # Binding uses rendered row identity, never a stale list index.
                for sequence, binding in getattr(c, "_title_bindings", ()):
                    c.tag_unbind("title", sequence, binding)
                c._title_bindings = []
                if i == 2 and title != safe_label(row):
                    enter = c.tag_bind("title", "<Enter>",
                               lambda event, k=row["key"], text=safe_label(row):
                               self._title_enter(event, k, text))
                    leave = c.tag_bind("title", "<Leave>", lambda _event: self._hide_tooltip())
                    c._title_bindings = [("<Enter>", enter), ("<Leave>", leave)]
                if i == 2:
                    count = row.get('subagent_count') or 0
                    counts = (row.get('child_activity') or {}).get('counts', {})
                    count_text = f'하위 {count}' if type(count) is int and 0 < count <= 9999 else ''
                    detail = ' / '.join(f'{label} {counts[key]}' for key, label in
                        (('read', '읽기'), ('write', '수정'), ('search', '검색'), ('memory', '기억'), ('execute', '실행')) if counts.get(key))
                    if count_text and not detail:
                        detail = '위임 작업 중'
                    if detail and count_text:
                        count_text += ' · ' + detail
                    while count_text and self.small_font.measure(count_text) > max(0, w - padding * 2 - reserve - 40):
                        if ' · ' in count_text:
                            count_text = count_text.split(' · ')[0]
                        else:
                            count_text = ''
                    count_width = self.small_font.measure(count_text) + 8 if count_text else 0
                    provider = (
                        row.get("provider")
                        if row.get("provider")
                        in ("claude", "codex", "antigravity", "copilot", "mcp")
                        else "session"
                    )
                    c.create_text(
                        padding,
                        60,
                        text=self._footer_text(
                            row,
                            w
                            - padding * 2
                            - reserve
                            - count_width,
                        ),
                        fill=muted_color(self.fg, self.bg),
                        font=self.small_font,
                        anchor="w",
                        tags="identity",
                    )
                    state = STATE_LABELS.get(
                        self.presentation.state(row), STATE_LABELS["unknown"]
                    ).split(" ", 1)[1]
                    if row.get('state') == 'working':
                        state = {'read': '읽는 중', 'write': '수정 중', 'search': '검색 중',
                                 'memory': '기억 조회', 'execute': '실행 중'}.get(row.get('activity_category'), state)
                    color = (
                        self.theme.get("echo_outline", self.fg)
                        if row.get("state") == "needs_input"
                        else (
                            "#e58d83"
                            if row.get("state") == "blocked"
                            else self.theme["tool_outline"]
                        )
                    )
                    c.create_text(
                        padding + 10,
                        15,
                        text="모니터링" if w >= 220 else "감시",
                        fill=(
                            self.theme["tool_outline"]
                            if self.selection.auto_enabled
                            else "#60717d"
                        ),
                        font=self.small_font,
                        anchor="w",
                        tags="monitoring",
                    )
                    self._add_graphic(
                        c,
                        "dot",
                        padding + 2,
                        15,
                        8,
                        (
                            self.theme["tool_outline"]
                            if self.selection.auto_enabled
                            else "#60717d"
                        ),
                        "monitoring_dot",
                    )
                    state_width = self.small_font.measure(state)
                    mode = "자동 ON" if self.selection.auto_enabled else "고정"
                    mode_right = w - padding
                    if not self.selection.auto_enabled:
                        pin_x = mode_right - self.small_font.measure(mode) - 18
                        self._add_graphic(
                            c, "pin", pin_x, 15, 15, "#60717d", "pin_icon"
                        )
                    self._add_mode_chip(
                        c,
                        mode_right,
                        15,
                        mode,
                        (
                            self.theme["tool_outline"]
                            if self.selection.auto_enabled
                            else "#60717d"
                        ),
                    )
                    c.create_text(
                        mode_right,
                        15,
                        text=mode,
                        fill=(
                            self.theme["tool_outline"]
                            if self.selection.auto_enabled
                            else "#60717d"
                        ),
                        font=self.small_font,
                        anchor="e",
                        tags="mode",
                    )
                    c.create_text(
                        w - 34,
                        60,
                        text=state,
                        fill=color,
                        font=self.small_font,
                        anchor="center",
                        tags="state",
                    )
                    self._add_graphic(
                        c,
                        self.presentation.state(row),
                        w - 34,
                        38,
                        22,
                        color,
                        "state_icon",
                    )
                    c.create_line(
                        18,
                        2,
                        48,
                        2,
                        width=2,
                        capstyle="round",
                        fill=(
                            self.theme["tool_outline"]
                            if self.selection.auto_enabled
                            else "#60717d"
                        ),
                        tags="mode_edge",
                    )
                    if count_text:
                        c.create_text(
                            w - 70,
                            60,
                            text=count_text,
                            fill=self.fg,
                            font=self.small_font,
                            anchor="e",
                            tags="subagents",
                        )
                else:
                    c.create_text(
                        12,
                        26 if i == 0 else h - 10,
                        text=self._footer_text(
                            row,
                            w - 24,
                        ),
                        fill="#bec8c7",
                        font=self.small_font,
                        anchor="w",
                        tags="identity",
                    )
            else:
                waiting = sum(r["state"] == "needs_input" for r in self.rows)
                blocked = sum(r["state"] == "blocked" for r in self.rows)
                items = (
                    (f"목록 {len(self.rows)}", "list", self.toggle_popup, True),
                    (
                        "자동 ON" if self.selection.auto_enabled else "자동 OFF",
                        "auto",
                        self.toggle_auto,
                        True,
                    ),
                    (
                        f"승인 {waiting}",
                        "waiting",
                        lambda: self.jump_attention("needs_input"),
                        waiting > 0,
                    ),
                    (
                        f"오류 {blocked}",
                        "blocked",
                        lambda: self.jump_attention("blocked"),
                        blocked > 0,
                    ),
                )
                tips = {
                    "waiting": "승인을 기다리는 세션으로 이동합니다.",
                    "blocked": "오류가 있는 세션으로 이동합니다.",
                    "auto": "자동 전환 ON/OFF · OFF에서도 상태 모니터링은 계속됩니다.",
                    "list": "현재 연결된 세션 · 휠 탐색 · 우클릭 이전/다음",
                }
                self._buttons = {}
                self._pressed_button = None
                columns = 4 if w >= 284 else 2
                button_w = (w - 16 - (columns - 1) * 4) / columns
                widths = (
                    [(w - 28) * part for part in (0.22, 0.35, 0.225, 0.205)]
                    if columns == 4
                    else [button_w] * 4
                )
                for number, (text, tag, callback, enabled) in enumerate(items):
                    left = 8 + (
                        sum(widths[:number]) + number * 4
                        if columns == 4
                        else (number % 2) * (button_w + 4)
                    )
                    top = 6 + (number // columns) * 28
                    right, bottom = left + widths[number], top + 24
                    cx, cy = (left + right) / 2, (top + bottom) / 2
                    common = f"button:{tag}"
                    # Rounded full hit area, not only a text-item event target.
                    r = 5
                    shape = c.create_polygon(
                        left + r,
                        top,
                        right - r,
                        top,
                        right,
                        top,
                        right,
                        top + r,
                        right,
                        bottom - r,
                        right,
                        bottom,
                        right - r,
                        bottom,
                        left + r,
                        bottom,
                        left,
                        bottom,
                        left,
                        bottom - r,
                        left,
                        top + r,
                        left,
                        top,
                        smooth=True,
                        splinesteps=16,
                        fill="#e0eeeb",
                        outline="#93b5ac",
                        width=1,
                        tags=(common, f"{tag}_hit"),
                    )
                    alert_state = {"waiting": "needs_input", "blocked": "blocked"}.get(
                        tag
                    )
                    emphasis = any(
                        r["state"] == alert_state and not r["acknowledged"]
                        for r in self.rows
                    )
                    image_id = c.create_image(
                        left, top, anchor="nw", tags=(common, f"{tag}_surface")
                    )
                    text_id = c.create_text(
                        left + (8 if tag == "auto" else 22),
                        cy,
                        text=text,
                        fill=self.fg,
                        font=self.small_font,
                        anchor="w",
                        tags=(tag, common),
                    )
                    self._buttons[tag] = {
                        "shape": shape,
                        "text": text_id,
                        "bounds": (left, top, right, bottom),
                        "enabled": enabled,
                        "callback": callback,
                        "image": image_id,
                        "emphasis": emphasis,
                    }
                    self._button_visual(tag, "normal")
                    # Keep acknowledgement emphasis without growing labels out of buttons.
                    if emphasis:
                        c.itemconfigure(
                            shape, outline=self.theme["tool_outline"], width=2
                        )
                    tip = (
                        tips[tag]
                        if enabled
                        else (
                            "현재 승인 대기 세션이 없습니다."
                            if tag == "waiting"
                            else "현재 오류 세션이 없습니다."
                        )
                    )
                    c.tag_bind(
                        common,
                        "<Enter>",
                        lambda e, t=tag, help_text=tip: self._button_enter(
                            e, t, help_text
                        ),
                    )
                    c.tag_bind(
                        common, "<Leave>", lambda _e, t=tag: self._button_leave(t)
                    )
                    c.tag_bind(
                        common,
                        "<ButtonPress-1>",
                        lambda _e, t=tag: self._button_press(t),
                    )
                    c.tag_bind(
                        common,
                        "<ButtonRelease-1>",
                        lambda e, t=tag: self._button_release(e, t),
                    )
                c.bind(
                    "<Button-3>",
                    lambda e: self._navigation_menu.tk_popup(e.x_root, e.y_root),
                )
            if (i < 2 and len(self.rows) == 1) or (i == 0 and len(self.rows) == 2):
                if win.winfo_viewable():
                    win.withdraw()
            else:
                if not win.winfo_viewable():
                    win.deiconify()
                # Tk geometry is deferred. A synchronous native NOMOVE restack
                # before it runs reports the old position back to Tk and can
                # cancel a horizontal move while our bounds already look new.
                if float(win.attributes("-alpha")) != alpha:
                    win.attributes("-alpha", alpha)
        # Center must cover the rear cards even while another application has focus.
        # Flush all deferred geometry before the one necessary native z-order pass.
        self.parent.update_idletasks()
        order = tuple(win for win in self.surfaces if win.winfo_viewable())
        order_changed = order != self._stack_order
        if order_changed:
            for win in order:
                keep_monitor_visible(win)
            # Toolbar stays above the center's soft shadow where their outer
            # rectangles overlap; the center is already above both rear cards.
            self._stack_order = order
        self._sync_shadow(force=order_changed)
        if self._animation:
            if progress >= 1:
                self._cancel_animation()
            elif self._animation_after is None:
                self._animation_after = self.parent.after(16, self._animation_frame)
        self._sync_mode_effect()

    def show(self, anchor, *, work_rect=None):
        if self._anchor != anchor:
            self._cancel_animation()
            self._display_key = None
        self._anchor = anchor
        self._work = work_rect or geometry.get_monitor_work_rect(anchor[0], anchor[1])
        self._visible = bool(self.rows)
        self._render()

    def set_overlay_foreground(self, foreground, below_hwnd=0, *, topmost=False):
        """Compatibility hook: bubble engagement must never demote the monitor."""
        self._foreground, self._topmost, self._below_hwnd = (
            bool(foreground),
            bool(topmost),
            int(below_hwnd),
        )
        self.restack_monitor()

    def restack_monitor(self):
        """Reassert persistent ordering on a native focus transition, never demote."""
        for win in self.surfaces:
            if win.winfo_viewable():
                keep_monitor_visible(win)
        if self._visible:
            self._sync_shadow(force=True)
        for win in (self.popup, self._editor):
            if win is not None and win.winfo_viewable():
                keep_popup_visible(win)
        if self._tooltip is not None and self._tooltip.winfo_viewable():
            keep_monitor_visible(self._tooltip)

    def toggle_popup(self):
        self._hide_tooltip()
        if self.popup is not None and self.popup.winfo_viewable():
            self.close_popup()
            return
        if not self._visible or not self.rows:
            return
        if self.popup is None:
            self.popup = tk.Toplevel(self.parent)
            self.popup.withdraw()
            self.popup.overrideredirect(True)
            self.popup.configure(bg=CHROMA)
            self.popup.attributes("-transparentcolor", CHROMA)
            self.popup.attributes("-alpha", 0.98)
            self.popup_canvas = tk.Canvas(self.popup, bg=CHROMA, highlightthickness=0)
            self.popup_canvas.pack(fill="both", expand=True)
            self.popup_canvas.bind(
                "<MouseWheel>",
                lambda event: self.page_popup(-1 if event.delta > 0 else 1),
            )
            self.popup.bind("<Escape>", lambda _event: self.close_popup())
            keep_monitor_visible(self.popup)
        self._popup_page = 0
        self._render_popup(opening=True)
        # Only this explicit user action sets Tk's keyboard target. It does not
        # force native activation or alter another application's foreground.
        self.popup_canvas.focus_set()

    def close_popup(self):
        if self.popup is not None:
            self.popup.withdraw()
        self.popup_rows = []

    def page_popup(self, delta):
        self._popup_page += delta
        self._render_popup()
        return "break"

    def select_from_popup(self, key):
        if key not in {row["key"] for row in self.rows}:
            return
        self.selection.pin(key)
        if self._settle_after is not None:
            self.parent.after_cancel(self._settle_after)
            self._settle_after = None
        self.close_popup()
        self._render()
        self.on_change()

    def _close_editor(self):
        if self._editor is not None:
            self._editor.destroy()
        self._editor = self._editing_key = None

    def edit_title(self, key):
        self._hide_tooltip()
        row = next((r for r in self.rows if r["key"] == key), None)
        if row is None or self.on_rename is None or row.get('is_bubble') is True:
            return
        self._close_editor()
        self._editing_key = key
        editor = self._editor = tk.Toplevel(self.parent)
        editor.withdraw()
        editor.title("세션 이름 편집")
        editor.configure(bg=self.bg)
        editor.resizable(False, False)
        tk.Label(
            editor,
            text="현재 연결된 세션 이름 (128자 이내)",
            bg=self.bg,
            fg=self.fg,
            font=self.font,
        ).pack(padx=12, pady=(10, 5))
        self._title_entry = tk.Entry(editor, font=self.font)
        self._title_entry.insert(0, row.get("label") or "")
        self._title_entry.pack(fill="x", padx=12)
        tk.Label(
            editor,
            text="직접 정한 이름은 연결이 끝날 때까지 유지됩니다. 비우고 저장하면 자동 제목으로 돌아갑니다.",
            wraplength=285,
            bg=self.bg,
            fg="#60716f",
            font=self.small_font,
        ).pack(padx=12, pady=5)
        self._title_error = tk.Label(
            editor, text="", bg=self.bg, fg="#963b36", font=self.small_font
        )
        self._title_error.pack()
        actions = tk.Frame(editor, bg=self.bg)
        actions.pack(pady=(0, 10))
        tk.Button(actions, text="저장", command=self.save_title).pack(
            side="left", padx=5
        )
        tk.Button(actions, text="취소", command=self._close_editor).pack(
            side="left", padx=5
        )
        editor.bind("<Return>", lambda _e: self.save_title())
        editor.bind("<Escape>", lambda _e: self._close_editor())
        editor.protocol("WM_DELETE_WINDOW", self._close_editor)
        editor.update_idletasks()
        width = min(320, self._work[2] - self._work[0])
        height = editor.winfo_reqheight()
        x, y = geometry.clamp_rect(
            self.bounds[2][0], self.bounds[2][1], width, height, self._work
        )
        editor.geometry(f"{width}x{height}+{x}+{y}")
        keep_popup_visible(editor)
        editor.deiconify()
        self._title_entry.focus_set()  # Explicit edit action, never passive refresh.

    def save_title(self):
        if self._editor is None:
            return False
        label = self._title_entry.get().strip() or None
        if not self.on_rename(self._editing_key, label):
            if self._editor is not None:
                self._title_error.configure(
                    text="이름을 확인하거나 연결 상태를 확인하세요."
                )
            return False
        self._close_editor()
        return True

    def _render_popup(self, *, opening=False):
        if self.popup is None or (not opening and not self.popup.winfo_viewable()):
            return
        if not self._visible or not self.rows:
            self.close_popup()
            return
        work = self._work
        width = min(380, work[2] - work[0])
        page_size = max(1, min(6, (work[3] - work[1] - 64) // 52))
        pages = (len(self.rows) + page_size - 1) // page_size
        self._popup_page = max(0, min(self._popup_page, pages - 1))
        self.popup_rows = self.rows[
            self._popup_page * page_size : (self._popup_page + 1) * page_size
        ]
        height = 64 + 52 * len(self.popup_rows)
        rail_x, rail_y, rail_w, _ = self.bounds[3]
        x = rail_x + rail_w + 8
        if x + width > work[2]:
            x = self.bounds[2][0] - width - 8
        x, y = geometry.clamp_rect(x, rail_y, width, height, work)
        self.popup_bounds = (x, y, width, height)
        signature = (
            self._content_signature(),
            self.selection.selected_key,
            self._popup_page,
            self.popup_bounds,
            tuple(self.theme.items()),
        )
        if not opening and signature == self._popup_signature:
            return
        self._popup_signature = signature
        canvas = self.popup_canvas
        canvas.configure(width=width, height=height)
        canvas.delete("all")
        self._card_background(canvas, width, height, True)
        canvas.create_text(
            14,
            20,
            text=f"연결된 세션 {len(self.rows)}",
            fill=self.fg,
            font=self.popup_heading_font,
            anchor="w",
        )
        canvas.create_text(
            width - 18,
            20,
            text="×",
            fill=self.fg,
            font=self.popup_heading_font,
            tags="close",
        )
        canvas.tag_bind("close", "<Button-1>", lambda _event: self.close_popup())
        for index, row in enumerate(self.popup_rows):
            top = 38 + index * 52
            tag = f"row_{index}"
            selected = row["key"] == self.selection.selected_key
            canvas.create_rectangle(
                8,
                top,
                width - 8,
                top + 48,
                fill=self.bg,
                outline=(
                    self.theme["tool_outline"]
                    if selected
                    else self.theme["speech_outline"]
                ),
                tags=tag,
            )
            canvas.create_text(
                16,
                top + 14,
                text="▶" if selected else "·",
                fill=self.fg,
                font=self.small_font,
                tags=tag,
            )
            canvas.create_text(
                30,
                top + 14,
                text=self._fit(safe_label(row), width - 140, self.font),
                fill=self.fg,
                font=self.font,
                anchor="w",
                tags=tag,
            )
            canvas.create_text(
                width - 16,
                top + 14,
                text=STATE_LABELS.get(self.presentation.state(row), STATE_LABELS["unknown"]),
                fill=self.fg,
                font=self.small_font,
                anchor="e",
                tags=tag,
            )
            provider = (
                row.get("provider")
                if row.get("provider")
                in ("claude", "codex", "copilot", "antigravity", "mcp")
                else "session"
            )
            canvas.create_text(
                30,
                top + 34,
                text=self._footer_text(row,width-140),
                fill=muted_color(self.fg, self.bg),
                font=self.small_font,
                anchor="w",
                tags=tag,
            )
            if self.on_rename is not None and row.get('is_bubble') is not True:
                edit_tag = f"edit_{index}"
                canvas.create_text(
                    width - 16,
                    top + 34,
                    text="이름 편집",
                    fill=self.theme["tool_outline"],
                    font=self.small_font,
                    anchor="e",
                    tags=edit_tag,
                )
                canvas.tag_bind(
                    edit_tag,
                    "<Button-1>",
                    lambda _e, key=row["key"]: self.edit_title(key),
                )
            canvas.tag_bind(
                tag,
                "<Button-1>",
                lambda _event, key=row["key"]: self.select_from_popup(key),
            )
        canvas.create_text(
            22, height - 14, text="‹", fill=self.fg, font=self.font, tags="previous"
        )
        canvas.create_text(
            width - 22, height - 14, text="›", fill=self.fg, font=self.font, tags="next"
        )
        canvas.create_text(
            width // 2,
            height - 14,
            text=f"{self._popup_page+1} / {pages}",
            fill=self.fg,
            font=self.small_font,
        )
        canvas.tag_bind("previous", "<Button-1>", lambda _event: self.page_popup(-1))
        canvas.tag_bind("next", "<Button-1>", lambda _event: self.page_popup(1))
        self.popup.geometry(f"{width}x{height}+{x}+{y}")
        if opening:
            self.popup.deiconify()
        self.popup.update_idletasks()
        keep_popup_visible(self.popup)

    def hide(self):
        self._hide_shadow()
        self._shadow_failures = 0
        self._shadow_retry_at = 0
        if self._mode_after is not None:
            self.parent.after_cancel(self._mode_after)
            self._mode_after = None
        self._close_editor()
        self._hide_tooltip()
        self._render_signature = self._popup_signature = self._stack_order = None
        self._visible = False
        self._cancel_animation()
        self._display_key = None
        self.close_popup()
        if self._settle_after is not None:
            self.parent.after_cancel(self._settle_after)
            self._settle_after = None
        self.selection.candidate_key = self.selection.deadline = None
        for win in self.surfaces:
            win.withdraw()

    def destroy(self):
        self.hide()
        if self.shadow is not None:
            self.shadow.destroy()
        self._navigation_menu.destroy()
        if self._tooltip is not None:
            self._tooltip.destroy()
        if self.popup is not None:
            self.popup.destroy()
        for win in self.surfaces:
            win.destroy()
