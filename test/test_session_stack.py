import tkinter as tk
import unittest
from unittest.mock import patch
from overlay.session_stack import (
    SessionStackWidget,
    safe_label,
    contrast_ratio,
    adjacent_position,
)


class DeckTests(unittest.TestCase):
    def test_clipped_title_hover_preview_and_cancellation(self):
        for title in ('긴 세션 제목을 마우스로 확인하는 테스트입니다 ' * 3,
                      'Long English session title preview for monitoring ' * 2):
            self.deck.selection.pin('0')
            self.rows[0]['label'] = title
            self.deck.update_rows(self.rows)
            self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
            self.root.update()
            canvas = self.deck.canvas
            box = canvas.bbox('title')
            canvas.event_generate('<Enter>', x=box[0] + 4, y=(box[1] + box[3]) // 2)
            canvas.event_generate('<Motion>', x=box[0] + 8, y=(box[1] + box[3]) // 2)
            self.root.update()
            self.assertIsNotNone(self.deck._tooltip_after, (canvas.gettags('current'), canvas.tag_bind('title', '<Enter>')))
            ready = tk.BooleanVar(self.root, False)
            self.root.after(400, lambda: ready.set(True))
            self.root.wait_variable(ready)
            self.root.update()
            self.assertTrue(self.deck._tooltip.winfo_viewable())
            self.assertEqual(self.deck._tooltip_label.cget('text'), title.strip())
            import os
            capture = os.environ.get('ENGRAM_TITLE_PREVIEW_CAPTURE')
            if capture and title.startswith('긴'):
                from PIL import ImageGrab
                from pathlib import Path
                tip = self.deck._tooltip
                Path(capture).parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(bbox=(tip.winfo_rootx(), tip.winfo_rooty(),
                                    tip.winfo_rootx() + tip.winfo_width(),
                                    tip.winfo_rooty() + tip.winfo_height())).save(capture)
            canvas.event_generate('<Leave>')
            self.root.update()
            self.assertFalse(self.deck._tooltip.winfo_viewable())
            canvas.event_generate('<Motion>', x=box[0] + 4, y=(box[1] + box[3]) // 2)
            self.root.update()
            self.deck.browse(1)
            self.assertIsNone(self.deck._tooltip_after)
            self.assertFalse(self.deck._tooltip.winfo_viewable())
            self.deck.hide()
            self.assertIsNone(self.deck._tooltip_after)
        # A title refresh while hover is pending cannot show the old title.
        from types import SimpleNamespace
        self.deck.selection.pin('0')
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        self.deck._title_enter(SimpleNamespace(x_root=620, y_root=500), '0', self.rows[0]['label'])
        self.rows[0]['label'] = 'Renamed session'
        self.deck.update_rows(self.rows)
        self.assertIsNone(self.deck._tooltip_after)
        self.assertFalse(self.deck._tooltip.winfo_viewable())
        self.deck.selection.pin('0')
        self.rows[0]['label'] = 'short'
        self.deck.update_rows(self.rows)
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        self.assertFalse(self.deck.canvas.tag_bind('title', '<Enter>'))

    def test_completion_caption_dwell_and_bubble_edit_disabled(self):
        from overlay.session_presentation import SessionPresentation
        now = [0]
        self.deck.presentation = SessionPresentation(clock=lambda: now[0])
        self.deck.selection.pin('0')
        self.deck.update_rows(self.rows)
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        self.rows[0].update(state='ready', state_since=2)
        self.deck.update_rows(self.rows)
        self.assertEqual(self.deck.canvas.itemcget('state', 'text'), '완료')
        now[0] = 2.01
        self.deck.update_rows(self.rows)
        self.assertEqual(self.deck.canvas.itemcget('state', 'text'), '대기')
        self.assertEqual(self.rows[0]['state'], 'ready')
        self.rows[0].update(is_bubble=True, label='오버레이 세션')
        self.deck.on_rename = lambda _key, _label: True
        self.deck.update_rows(self.rows)
        self.deck.toggle_popup()
        self.assertFalse(self.deck.popup_canvas.find_withtag('edit_0'))
        self.deck.edit_title('0')
        self.assertIsNone(self.deck._editor)

    def test_shadow_failures_are_bounded_and_never_stop_monitoring(self):
        with patch(
            "overlay.session_stack.SessionShadow", side_effect=OSError("fixture")
        ) as create:
            with patch("overlay.session_stack.time.monotonic") as clock:
                for instant in (1000, 1000.3, 1000.6, 1001, 1002):
                    clock.return_value = instant
                    self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
                self.assertEqual(create.call_count, 3)
                self.assertTrue(self.deck.win.winfo_viewable())
                self.assertIsNone(self.deck.shadow)
        self.deck.hide()
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        shadow = self.deck.shadow
        with patch.object(shadow, "_upload", side_effect=OSError("fixture")):
            self.deck.show((610, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
            self.assertTrue(self.deck.win.winfo_viewable())
            self.assertFalse(shadow.visible)
        self.deck._shadow_retry_at = 0
        self.deck.show((610, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        self.assertTrue(shadow.visible)

    def test_native_shadow_owned_clickthrough_bounded_and_no_gdi_leak(self):
        import ctypes
        import win32gui
        from overlay.bubble.bubble_window import _toplevel_hwnd

        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        shadow = self.deck.shadow
        hwnd = shadow.hwnd
        style = win32gui.GetWindowLong(hwnd, -20)
        self.assertEqual(
            style & (0x80000 | 0x20 | 0x08000000 | 0x80),
            0x80000 | 0x20 | 0x08000000 | 0x80,
        )
        self.assertEqual(win32gui.GetWindow(hwnd, 4), 0)
        import os, win32process

        self.assertEqual(win32process.GetWindowThreadProcessId(hwnd)[1], os.getpid())
        self.assertEqual(win32gui.SendMessage(hwnd, 0x84, 0, 0), -1)
        self.assertEqual(win32gui.GetWindow(hwnd, 3), _toplevel_hwnd(self.deck.win))
        user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        user.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        process = kernel.GetCurrentProcess()
        baseline = user.GetGuiResources(process, 0)
        for offset in range(30):
            shadow.update((0, 0, 220 + offset, 76), (0, 0, 1600, 1000))
            self.assertGreaterEqual(min(shadow.bounds[:2]), 0)
        self.assertLessEqual(user.GetGuiResources(process, 0), baseline + 1)
        shadow.update(self.deck.win._native_bounds, self.deck._work)
        with patch.object(shadow, "_upload", wraps=shadow._upload) as upload:
            self.deck.update_rows(self.rows)
            self.tick(0.15)
            upload.assert_not_called()
        self.deck.hide()
        self.assertFalse(win32gui.IsWindowVisible(hwnd))
        shadow.destroy()
        self.assertFalse(win32gui.IsWindow(hwnd))

    def test_supersampled_pill_states_and_distinct_status_icons(self):
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        auto = self.deck._buttons["auto"]
        on_photo = str(auto["photo"])
        self.deck.toggle_auto()
        self.assertNotEqual(on_photo, str(self.deck._buttons["auto"]["photo"]))
        from PIL import Image, ImageDraw

        icons = []
        for name in ("working", "needs_input", "ready", "blocked", "unknown"):
            bitmap = Image.new("RGBA", (88, 88))
            self.deck._draw_graphic(
                ImageDraw.Draw(bitmap), name, (2, 2, 18, 18), "#123456"
            )
            icons.append(bitmap.tobytes())
        self.assertEqual(len(set(icons)), 5)

    def test_mode_header_effect_is_coords_only_and_cancels(self):
        from contextlib import ExitStack

        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        self.tick(0.05)
        for width in (135, 270, 540):
            self.deck.show((600, 620, width, 302), work_rect=(0, 0, 1600, 1000))
            canvas = self.deck.canvas
            boxes = [canvas.bbox(tag) for tag in ("monitoring", "mode")]
            self.assertLess(canvas.bbox("title")[2], canvas.bbox("state_icon")[0])
            self.assertTrue(all(box[1] == boxes[0][1] for box in boxes))
            self.assertTrue(all(a[2] < b[0] for a, b in zip(boxes, boxes[1:])))
        with ExitStack() as patches:
            paints = [
                patches.enter_context(patch.object(c, "delete", wraps=c.delete))
                for c in self.deck.canvases
            ]
            moves = [
                patches.enter_context(patch.object(w, "geometry", wraps=w.geometry))
                for w in self.deck.surfaces
            ]
            restack = patches.enter_context(
                patch("overlay.session_stack.keep_monitor_visible")
            )
            coords = patches.enter_context(
                patch.object(self.deck.canvas, "coords", wraps=self.deck.canvas.coords)
            )
            self.tick(0.3)
            self.assertGreater(coords.call_count, 2)
            self.assertFalse(any(p.call_count for p in paints + moves))
            restack.assert_not_called()
        self.deck.toggle_auto()
        self.assertFalse(self.deck.selection.auto_enabled)
        self.assertIsNone(self.deck._mode_after)
        self.assertEqual("고정", self.deck.canvas.itemcget("mode", "text"))
        self.assertTrue(self.deck.canvas.find_withtag("pin_icon"))
        self.assertIn("OFF", self.deck.canvases[3].itemcget("auto", "text"))
        before = self.deck.canvas.coords("mode_edge")
        self.tick(0.2)
        self.assertEqual(before, self.deck.canvas.coords("mode_edge"))
        self.deck.resume_auto()
        self.assertIsNotNone(self.deck._mode_after)
        self.deck.hide()
        self.assertIsNone(self.deck._mode_after)

    def test_toolbar_button_feedback_hitarea_and_no_card_churn(self):
        from contextlib import ExitStack

        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        rail = self.deck.canvases[3]
        self.assertEqual(self.deck.bounds[3][3], 36)
        self.tick(0.05)
        with ExitStack() as patches:
            paints = [
                patches.enter_context(patch.object(c, "delete", wraps=c.delete))
                for c in self.deck.canvases[:3]
            ]
            restack = patches.enter_context(
                patch("overlay.session_stack.keep_monitor_visible")
            )
            button = self.deck._buttons["list"]
            x, y = int(button["bounds"][0] + 5), int(button["bounds"][1] + 12)
            normal = rail.itemcget(button["shape"], "fill")
            rail.event_generate("<Motion>", x=x, y=y)
            self.assertEqual(rail.cget("cursor"), "hand2")
            self.assertNotEqual(normal, rail.itemcget(button["shape"], "fill"))
            hover = rail.itemcget(button["shape"], "fill")
            rail.event_generate("<ButtonPress-1>", x=x, y=y)
            self.assertNotEqual(hover, rail.itemcget(button["shape"], "fill"))
            restack.assert_not_called()
            rail.event_generate("<ButtonRelease-1>", x=x, y=y)
            self.assertTrue(self.deck.popup.winfo_viewable())
            self.assertFalse(any(p.call_count for p in paints))
            self.assertFalse(
                any(
                    call.args[0] in self.deck.surfaces
                    for call in restack.call_args_list
                )
            )
        self.deck.update_rows([dict(self.rows[0], state="working")])
        before = self.deck.selection.selected_key
        button = self.deck._buttons["blocked"]
        self.assertFalse(button["enabled"])
        x, y = int(button["bounds"][0] + 5), int(button["bounds"][1] + 12)
        rail.event_generate("<Motion>", x=x, y=y)
        rail.event_generate("<ButtonPress-1>", x=x, y=y)
        rail.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.assertEqual(rail.cget("cursor"), "arrow")
        self.assertEqual(self.deck.selection.selected_key, before)
        self.assertIsNone(self.deck.selection.pinned_key)
        self.deck.show((600, 620, 135, 151), work_rect=(0, 0, 1600, 1000))
        self.assertEqual(self.deck.bounds[3][3], 64)

    def test_static_heartbeat_never_paints_moves_or_restacks(self):
        from contextlib import ExitStack

        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        with ExitStack() as patches:
            paints = [
                patches.enter_context(patch.object(c, "delete", wraps=c.delete))
                for c in self.deck.canvases
            ]
            moves = [
                patches.enter_context(patch.object(w, "geometry", wraps=w.geometry))
                for w in self.deck.surfaces
            ]
            restack = patches.enter_context(
                patch("overlay.session_stack.keep_monitor_visible")
            )
            for _ in range(13):
                for row in self.rows:
                    row["last_seen"] += 1
                self.deck.update_rows(self.rows)
                self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
            self.assertFalse(any(call.call_count for call in paints + moves))
            restack.assert_not_called()
            self.rows[0]["state"] = "ready"
            self.deck.update_rows(self.rows)
            self.assertTrue(all(call.call_count == 1 for call in paints))
            self.assertFalse(any(call.call_count for call in moves))
            restack.assert_not_called()

    def test_named_toolbar_tooltip_bounds_and_hide_lifecycle(self):
        from types import SimpleNamespace

        self.deck.show((600, 620, 135, 151), work_rect=(0, 0, 1000, 900))
        rail = self.deck.canvases[3]
        for tag, label in [
            ("list", "목록"),
            ("auto", "자동"),
            ("waiting", "승인"),
            ("blocked", "오류"),
        ]:
            self.assertIn(label, rail.itemcget(tag, "text"))
            self.assertLessEqual(rail.bbox(tag)[2], 220)
        self.deck._queue_tooltip(
            SimpleNamespace(x_root=980, y_root=880), "휠로 세션을 이동합니다."
        )
        self.tick(0.4)
        self.assertTrue(self.deck._tooltip.winfo_viewable())
        self.assertLessEqual(
            self.deck._tooltip.winfo_x() + self.deck._tooltip.winfo_width(), 1000
        )
        self.assertLessEqual(
            self.deck._tooltip.winfo_y() + self.deck._tooltip.winfo_height(), 900
        )
        self.deck.hide()
        self.assertFalse(self.deck._tooltip.winfo_viewable())
        self.assertIsNone(self.deck._tooltip_after)

    def test_missing_title_is_honest_and_explicit_edit_does_not_select(self):
        self.assertEqual(safe_label({"key": "a", "provider": "mcp"}), "새 세션")
        self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
        before = self.deck.selection.selected_key
        edited = []
        self.deck.on_rename = lambda key, label: edited.append((key, label)) or True
        self.deck.edit_title("0")
        self.deck._title_entry.delete(0, "end")
        self.deck._title_entry.insert(0, "명시적 제목")
        self.assertTrue(self.deck.save_title())
        self.assertEqual(edited, [("0", "명시적 제목")])
        self.assertEqual(self.deck.selection.selected_key, before)
        self.assertIsNone(self.deck.selection.pinned_key)
        self.tick(0.1)  # Let deliberate editor close finish native focus restoration.

    def test_private_palette_and_compact_row_cardinality(self):
        purple = {"speech_bg": "#dbcafb", "speech_fg": "#400040"}
        self.deck.update_theme(purple)
        self.assertEqual(self.deck.bg, "#f4f0e8")
        self.assertGreaterEqual(contrast_ratio(self.deck.fg, "#ebf0ee"), 7)
        # Responsive toolbar replaces the old 108/150/192px vertical-rail layout.
        for count, height, cards in ((1, 116, 1), (2, 144, 2), (3, 176, 3)):
            self.deck.update_rows(self.rows[:count])
            self.deck.show((600, 620, 270, 302), work_rect=(0, 0, 1600, 1000))
            self.root.update_idletasks()
            visible = [w for w in self.deck.surfaces if w.winfo_viewable()]
            visible_cards = [w for w in self.deck.surfaces[:3] if w.winfo_viewable()]
            self.assertEqual(len(visible_cards), cards)
            self.assertEqual(len({w._card_key for w in visible_cards}), cards)
            self.assertLess(
                self.deck.canvas.bbox("title")[3], self.deck.canvas.bbox("identity")[1]
            )
            self.assertEqual(
                max(w.winfo_y() + w.winfo_height() for w in visible)
                - min(w.winfo_y() for w in visible),
                height,
            )

    def test_membership_changes_during_roll_cancel_and_keep_distinct_visible_cards(
        self,
    ):
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        for count in (2, 1, 3, 2, 3, 1):
            self.deck.browse(1)
            self.tick(0.04)
            self.deck.update_rows(self.rows[:count])
            self.root.update_idletasks()
            self.assertIsNone(self.deck._animation_after)
            visible = [w for w in self.deck.surfaces[:3] if w.winfo_viewable()]
            self.assertEqual(len(visible), count)
            self.assertEqual(len({w._card_key for w in visible}), count)
        self.assertEqual(
            max(
                w.winfo_y() + w.winfo_height()
                for w in self.deck.surfaces
                if w.winfo_viewable()
            ),
            608,
        )

    def tick(self, seconds):
        import time

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.005)

    def native_cards(self):
        import win32gui
        from overlay.bubble.bubble_window import _toplevel_hwnd

        return {
            win._card_key: (
                _toplevel_hwnd(win),
                win32gui.GetWindowRect(_toplevel_hwnd(win)),
                float(win.attributes("-alpha")),
            )
            for win in self.deck.surfaces[:3]
        }

    def test_roll_preserves_native_card_identity_and_reverse_pose(self):
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        before = self.native_cards()
        center_key = self.deck.win._card_key
        next_key = self.deck.surfaces[1]._card_key
        self.deck.browse(1)
        self.tick(0.055)
        middle = self.native_cards()
        self.assertEqual(before[center_key][0], middle[center_key][0])
        self.assertNotEqual(before[center_key][1], middle[center_key][1])
        self.assertNotEqual(before[next_key][1], middle[next_key][1])
        self.assertIsNone(self.deck.selection.pinned_key)
        self.deck.browse(-1)
        reversed_start = self.native_cards()[center_key]
        self.assertEqual(reversed_start[0], middle[center_key][0])
        self.assertLessEqual(abs(reversed_start[1][1] - middle[center_key][1][1]), 3)
        self.tick(0.25)
        self.assertEqual(self.deck.win._card_key, center_key)
        self.assertEqual(self.native_cards()[center_key][0], before[center_key][0])
        self.assertIsNone(self.deck._animation_after)

    def test_animation_removal_hide_single_and_two_row_fallback(self):
        self.deck.update_rows(self.rows[:2])
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        before = [
            (window.winfo_y(), float(window.attributes("-alpha")))
            for window in self.deck.surfaces[:3]
        ]
        self.deck.browse(1)
        self.assertTrue(
            all(plan["recycle"] for plan in self.deck._animation["plans"].values())
        )
        self.tick(0.05)
        self.assertTrue(
            any(
                window.winfo_y() != pose[0]
                and float(window.attributes("-alpha")) < pose[1]
                for window, pose in zip(self.deck.surfaces[:3], before)
            )
        )
        self.deck.update_rows(self.rows[:1])
        self.assertIsNone(self.deck._animation_after)
        self.deck.browse(1)
        self.assertIsNone(self.deck._animation)
        self.deck.update_rows(self.rows)
        self.deck.browse(1)
        self.deck.hide()
        self.assertIsNone(self.deck._animation_after)
        self.assertIsNone(self.deck._settle_after)

    def test_departing_card_identity_changes_only_after_zero_alpha(self):
        rows = self.rows + [dict(key="3", first_seen=3, last_seen=3, state="ready")]
        self.deck.update_rows(rows)
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        departing = self.deck.surfaces[0]
        old_key = departing._card_key
        native_attributes = departing.attributes
        hidden_keys = []

        def attributes(*args):
            result = native_attributes(*args)
            if args == ("-alpha", 0):
                hidden_keys.append(
                    (departing._card_key, float(native_attributes("-alpha")))
                )
            return result

        with patch.object(departing, "attributes", side_effect=attributes):
            self.deck.browse(1)
            self.tick(0.25)
        self.assertIn((old_key, 0), hidden_keys)
        self.assertNotEqual(departing._card_key, old_key)

    def test_popup_live_paging_selection_privacy_and_shell_ownership(self):
        rows = [
            dict(self.rows[i % 3], key=str(i), first_seen=i, last_seen=i)
            for i in range(9)
        ]
        rows[0]["label"] = "C:/private"
        self.deck.update_rows(rows)
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        self.assertIsNone(self.deck.popup)
        self.deck.toggle_popup()
        self.assertIs(self.deck.popup.master, self.root)
        self.assertEqual(len(self.deck.popup_rows), 6)
        self.assertNotIn(
            "private",
            "".join(
                self.deck.popup_canvas.itemcget(item, "text")
                for item in self.deck.popup_canvas.find_all()
                if self.deck.popup_canvas.type(item) == "text"
            ),
        )
        self.deck.page_popup(1)
        self.assertEqual(len(self.deck.popup_rows), 3)
        self.deck.select_from_popup("7")
        self.assertEqual(self.deck.selection.selected_key, "7")
        self.assertFalse(self.deck.popup.winfo_viewable())
        self.deck.toggle_popup()
        self.deck.update_rows(rows[:1])
        self.assertEqual([row["key"] for row in self.deck.popup_rows], ["0"])
        self.deck.update_rows([])
        self.assertFalse(self.deck.popup.winfo_viewable())

    def test_visible_shell_survives_cache_eviction(self):
        self.deck.show((600, 620, 240, 280), work_rect=(0, 0, 1600, 1000))
        canvas = self.deck.canvases[0]
        image_name = str(canvas._shell_image)
        for width in range(240, 270):
            self.deck._card_background(self.deck.canvas, width, 100, True)
        self.assertIn(image_name, self.root.tk.call("image", "names"))

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.deck = SessionStackWidget(self.root)
        self.rows = [
            dict(
                key=str(i),
                provider="claude",
                first_seen=i,
                last_seen=i,
                state=state,
                label="safe " + str(i),
            )
            for i, state in enumerate(("working", "needs_input", "blocked"))
        ]
        self.deck.update_rows(self.rows)

    def tearDown(self):
        self.deck.destroy()

    def test_parent_alpha_above_and_privacy(self):
        self.deck.show((600, 500, 48, 48), work_rect=(0, 0, 1000, 800))
        self.root.update()
        self.assertTrue(all(w.master is self.root for w in self.deck.surfaces))
        self.assertTrue(all(y + h <= 500 for x, y, w, h in self.deck.bounds))
        self.assertLess(
            float(self.deck.surfaces[0].attributes("-alpha")),
            float(self.deck.win.attributes("-alpha")),
        )
        self.assertGreaterEqual(contrast_ratio(self.deck.fg, self.deck.bg), 7)
        self.assertNotIn("secret", safe_label(dict(key="id", label="C:/secret")))

    def test_project_footer_prioritizes_name_and_reserves_identity(self):
        row = dict(self.rows[0], project_name="LongCallerProject" * 3)
        text = self.deck._footer_text(row, 135)
        from overlay.session_stack import safe_identity
        self.assertTrue(text.endswith(safe_identity(row)))
        self.assertTrue(text.startswith("Claude · "))
        self.assertIn("…", text)
        self.assertLessEqual(self.deck.small_font.measure(text), 135)
        row["project_name"] = "C:/private"
        self.assertNotIn("private", self.deck._footer_text(row, 135))

    def test_footer_provider_is_truthful_with_or_without_project(self):
        for provider, expected in (("claude", "Claude"), ("codex", "Codex"),
                                   ("copilot", "Copilot"), ("mcp", "MCP"),
                                   ("untrusted-provider", "MCP")):
            for project in (None, "Project"):
                row = dict(self.rows[0], provider=provider, project_name=project)
                text = self.deck._footer_text(row, 240)
                self.assertTrue(text.startswith(expected + " · "))
                if project:
                    self.assertIn(" · Project · ", text)

    def test_footer_uses_only_allowlisted_participant_agent_claim(self):
        row = dict(self.rows[0], provider="mcp", agent_name="Claude", project_name="Project")
        self.assertTrue(self.deck._footer_text(row, 240).startswith("Claude · "))
        self.assertEqual(row["provider"], "mcp")
        row["agent_name"] = "C:/private"
        self.assertTrue(self.deck._footer_text(row, 240).startswith("MCP · "))

    def test_debounce_replaces_timer_and_hide_cancels(self):
        self.deck.browse(1)
        first = self.deck._settle_after
        self.deck.browse(1)
        self.assertNotEqual(first, self.deck._settle_after)
        self.assertNotIn(first, self.root.tk.call("after", "info"))
        self.deck.hide()
        self.assertIsNone(self.deck._settle_after)

    def test_hidden_background_stays_topmost_and_negative_bounds(self):
        with patch("overlay.session_stack.keep_monitor_visible") as keep:
            self.deck.set_overlay_foreground(False, 123)
            self.deck.show((-1500, 400, 48, 48), work_rect=(-1920, 0, 0, 1040))
            self.assertGreaterEqual(keep.call_count, 4)
            self.assertLess(self.deck.win.winfo_x(), 0)

    def test_monitor_topmost_noactivate_and_center_right_status(self):
        import ctypes
        from overlay.bubble.bubble_window import _toplevel_hwnd

        foreground = ctypes.windll.user32.GetForegroundWindow()
        self.deck.show((600, 500, 48, 48), work_rect=(0, 0, 1000, 800))
        self.root.update()
        self.deck.set_overlay_foreground(False, 123)
        self.assertEqual(ctypes.windll.user32.GetForegroundWindow(), foreground)
        for win in self.deck.surfaces:
            style = ctypes.windll.user32.GetWindowLongW(_toplevel_hwnd(win), -20)
            self.assertTrue(style & 0x08000000)
            self.assertTrue(style & 8)
        self.assertGreater(
            self.deck.canvas.bbox("state")[0], self.deck.win.winfo_width() / 2
        )

    def test_narrow_bounds_fit(self):
        for row in self.rows:
            row["provider"] = "antigravity"
        self.deck.update_rows(self.rows)
        self.deck.show((100, 200, 48, 48), work_rect=(0, 0, 220, 400))
        self.assertTrue(all(x >= 0 and x + w <= 220 for x, y, w, h in self.deck.bounds))
        for canvas, bounds in zip(self.deck.canvases[:2], self.deck.bounds[:2]):
            self.assertLessEqual(canvas.bbox("identity")[2], bounds[2])

    def test_native_windows_follow_moved_anchor_and_right_edge(self):
        import win32gui
        from overlay.bubble.bubble_window import _toplevel_hwnd

        work = (0, 0, 1600, 1000)
        for anchor in (
            (600, 620, 240, 280),
            (900, 700, 240, 280),
            (1356, 700, 240, 280),
        ):
            self.deck.show(anchor, work_rect=work)
            self.root.update_idletasks()
            for window, bounds in zip(self.deck.surfaces, self.deck.bounds):
                x, y, width, height = bounds
                self.assertEqual(
                    win32gui.GetWindowRect(_toplevel_hwnd(window)),
                    (x, y, x + width, y + height),
                )
        self.assertEqual(
            max(
                win32gui.GetWindowRect(_toplevel_hwnd(window))[2]
                for window in self.deck.surfaces
            ),
            work[2],
        )

    def test_minimum_width_state_and_count_do_not_overlap(self):
        self.rows[1]["subagent_count"] = 9999
        self.deck.update_rows(self.rows)
        self.deck.selection.pin("1")
        self.deck.show((100, 200, 48, 48), work_rect=(0, 0, 190, 400))
        self.root.update_idletasks()
        state = self.deck.canvas.bbox("state")
        count = self.deck.canvas.bbox("subagents")
        self.assertIsNotNone(state)
        self.assertLess(count[2], state[0])


if __name__ == "__main__":
    unittest.main()
