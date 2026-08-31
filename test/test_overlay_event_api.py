import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from overlay.character import CharacterOverlay
from overlay.event_api import DISPLAY_HINTS, OverlayEventPublisher, event_for_bubble, tool_category
from overlay.main import OverlayApp
from overlay.bubble.bubble_manager import BubbleManager
from overlay.settings_window import OVERLAY_EVENT_API_MANUAL_URL, open_overlay_event_api_manual


class OverlayEventApiTests(unittest.TestCase):
    def test_manifest_compatible_hints_and_metadata_only_mapping(self):
        expected = {
            "idle", "hover", "click", "input", "generating", "search",
            "thought", "memory", "success", "provider_error", "error",
        }
        self.assertLessEqual(expected, DISPLAY_HINTS)
        self.assertEqual(
            event_for_bubble(
                {"kind": "tool_use", "tool_name": "web_search", "tool_input": {"secret": "no"}}
            ),
            ("tool.started", "search", {"category": "search"}),
        )
        self.assertEqual(
            event_for_bubble({"kind": "thought", "text": "private"}),
            ("generation.thinking", "thought", {}),
        )
        self.assertEqual(tool_category("kg_search"), "memory")
        self.assertEqual(tool_category("read_file"), "read")

    def test_child_handshake_snapshot_and_jsonl_event(self):
        class HeldStdout:
            def __init__(self):
                self.release = threading.Event()

            def __iter__(self):
                yield '{"type":"overlay.hello","payload":{"supported_schema_versions":[1]}}\n'
                self.release.wait(1.0)

            def close(self):
                self.release.set()

        proc = Mock()
        proc.stdout = HeldStdout()
        proc.stdin = io.StringIO()
        proc.poll.return_value = None
        with patch("overlay.event_api.subprocess.Popen", return_value=proc):
            publisher = OverlayEventPublisher(
                {"overlay": {"external_renderer": {"command": ["renderer.exe", "--jsonl"]}}}
            )
            self.assertTrue(publisher.start())
            publisher.publish("tool.started", "search", {"category": "search"})

        messages = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual([message["type"] for message in messages[:2]], ["engram.welcome", "state.snapshot"])
        self.assertEqual(messages[1]["display_hint"], "idle")
        self.assertEqual(messages[1]["payload"], {"generation_active": False, "tool_category": None})
        self.assertEqual(messages[-1]["payload"], {"category": "search"})
        self.assertEqual(messages[-1]["sequence"], 3)
        self.assertEqual(messages[-1]["schema_version"], 1)
        publisher.stop()

    def test_invalid_child_handshake_falls_back_without_raising(self):
        proc = Mock()
        proc.stdout = io.StringIO("not-json\n")
        proc.stdin = io.StringIO()
        proc.poll.return_value = None
        failed = []
        with patch("overlay.event_api.subprocess.Popen", return_value=proc):
            publisher = OverlayEventPublisher(
                {"overlay": {"external_renderer": {"command": ["renderer.exe"]}}},
                on_failure=lambda: failed.append(True),
            )
            self.assertFalse(publisher.start())
        self.assertEqual(failed, [True])

    def test_stdout_eof_fails_even_when_poll_has_not_observed_exit(self):
        proc = Mock()
        proc.stdout = io.StringIO("")
        proc.poll.return_value = None  # Reproduces the Windows EOF/poll race.
        failed = []
        publisher = OverlayEventPublisher(on_failure=lambda: failed.append(True))
        publisher._proc = proc
        publisher._read_stdout()
        self.assertTrue(publisher._failed)
        self.assertEqual(failed, [True])

    def test_stdout_eof_after_intentional_stop_does_not_fail(self):
        proc = Mock()
        proc.stdout = io.StringIO("")
        proc.poll.return_value = None
        failed = []
        publisher = OverlayEventPublisher(on_failure=lambda: failed.append(True))
        publisher._proc = proc
        publisher._stopping = True
        publisher._read_stdout()
        self.assertFalse(publisher._failed)
        self.assertEqual(failed, [])

    def test_replace_mode_exposes_inbound_geometry_callbacks(self):
        proc = Mock()
        proc.stdout = io.StringIO(
            '{"type":"overlay.hello","payload":{"supported_schema_versions":[1]}}\n'
            '{"schema_version":1,"type":"overlay.geometry_changed",'
            '"payload":{"x":1,"y":2,"width":30,"height":40}}\n'
        )
        proc.stdin = io.StringIO()
        proc.poll.return_value = None
        received = []
        with patch("overlay.event_api.subprocess.Popen", return_value=proc):
            publisher = OverlayEventPublisher(
                {
                    "overlay": {
                        "external_renderer": {"mode": "replace", "command": ["renderer.exe"]}
                    }
                },
                on_message=received.append,
            )
            self.assertTrue(publisher.start())
            self.assertEqual(publisher.mode, "replace")
            time.sleep(0.01)
        self.assertTrue(any(item.get("type") == "overlay.geometry_changed" for item in received))

    def test_real_child_process_jsonl_round_trip(self):
        child = (
            "import json,sys\n"
            "print(json.dumps({'type':'overlay.hello','payload':"
            "{'supported_schema_versions':[1]}}), flush=True)\n"
            "for _ in range(3): json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'schema_version':1,'type':'overlay.geometry_changed',"
            "'payload':{'x':11,'y':22,'width':33,'height':44}}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        received = []
        publisher = OverlayEventPublisher(
            {
                "overlay": {
                    "external_renderer": {
                        "mode": "replace",
                        "command": [sys.executable, "-u", "-c", child],
                    }
                }
            },
            on_message=received.append,
        )
        try:
            self.assertTrue(publisher.start())
            publisher.publish("tool.started", "search", {"category": "search"})
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                item.get("type") == "overlay.geometry_changed" for item in received
            ):
                time.sleep(0.01)
            self.assertTrue(any(item.get("type") == "overlay.geometry_changed" for item in received))
        finally:
            publisher.stop()

    def test_external_geometry_becomes_anchor_and_restores_bundled_rect(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 10
        overlay.root.winfo_y.return_value = 20
        overlay._img_w = 30
        overlay._img_h = 40
        overlay._external_rect = None
        saved = {}

        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch(
                "overlay.character.bubble_geometry.get_monitor_work_rect",
                return_value=(0, 0, 1920, 1080),
            ),
            patch("overlay.character.update_overlay_state", side_effect=lambda update: update(saved)),
        ):
            overlay.hide_for_external_renderer()
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400),
                (100, 200, 300, 400),
            )

        self.assertEqual(overlay.get_phys_rect(), (100, 200, 300, 400))
        self.assertEqual(saved["overlay_window"]["x"], 100)
        overlay.restore_bundled_renderer()
        self.assertEqual(overlay.get_phys_rect(), (10, 20, 30, 40))

    def test_initial_replace_geometry_keeps_saved_position_without_persisting(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 10
        overlay.root.winfo_y.return_value = 20
        overlay._img_w = 30
        overlay._img_h = 40
        overlay._external_rect = (500, 600, 30, 40)
        saved = {}
        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch("overlay.character.update_overlay_state") as update_state,
        ):
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400, preserve_position=True),
                (500, 600, 300, 400),
            )
        update_state.assert_not_called()
        self.assertEqual(saved, {})

    def test_external_drag_move_updates_anchor_without_writing_state(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._external_rect = (10, 20, 30, 40)
        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch("overlay.character.update_overlay_state") as update_state,
        ):
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400, persist_position=False),
                (100, 200, 300, 400),
            )
        update_state.assert_not_called()

    def test_replace_drag_move_updates_rect_without_persisting_or_acknowledging(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 70, 80)
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)

        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "pointer.action",
            "payload": {"action": "drag_move", "screen_x": 50, "screen_y": 60},
        })

        app.character.apply_external_geometry.assert_called_once_with(
            50, 60, 70, 80, persist_position=False
        )
        app._overlay_events.publish.assert_not_called()

    def test_replace_drag_end_persists_and_acknowledges_final_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 70, 80)
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)

        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "pointer.action",
            "payload": {"action": "drag_end", "screen_x": 50, "screen_y": 60},
        })

        app.character.apply_external_geometry.assert_called_once_with(
            50, 60, 70, 80, persist_position=True
        )
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 50, "y": 60}
        )

    def test_observer_geometry_is_transient_and_click_selects_shared_anchor(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app.character.get_bundled_phys_rect.return_value = (1, 2, 30, 40)
        app._bubble_manager = Mock()
        app._observer_rect = None
        app._bubble_anchor = "bundled"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 100, "y": 200, "width": 300, "height": 400},
        })
        self.assertEqual(app._observer_rect, (100, 200, 300, 400))
        app.character.apply_external_geometry.assert_not_called()
        self.assertEqual(app._get_bubble_anchor_rect(), (1, 2, 30, 40))

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        self.assertEqual(app._bubble_anchor, "observer")
        self.assertEqual(app._get_bubble_anchor_rect(), (100, 200, 300, 400))
        app.character.external_activate.assert_called_once()

    def test_active_observer_geometry_reflows_all_shared_surfaces(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_history = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 500, "y": 600, "width": 50, "height": 60},
        })
        self.assertEqual(app._observer_rect, (500, 600, 50, 60))
        app._bubble_manager.refresh_positions.assert_called_once()
        app._bubble_input.refresh_position.assert_called_once()
        app._bubble_history.refresh_position.assert_called_once()

    def test_renderer_failure_drops_observer_anchor_even_when_restore_raises(self):
        app = object.__new__(OverlayApp)
        app.character = Mock()
        app.character.restore_bundled_renderer.side_effect = RuntimeError("gone")
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_history = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"

        app._restore_bundled_renderer()
        self.assertIsNone(app._observer_rect)
        self.assertEqual(app._bubble_anchor, "bundled")
        app._bubble_manager.refresh_positions.assert_called_once()
        app._bubble_input.refresh_position.assert_called_once()
        app._bubble_history.refresh_position.assert_called_once()

    def test_replace_geometry_still_persists_and_acknowledges_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)
        app._observer_rect = None
        app._bubble_anchor = "bundled"
        app._replace_startup_geometry_pending = False

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 50, "y": 60, "width": 70, "height": 80},
        })
        app.character.apply_external_geometry.assert_called_once_with(50, 60, 70, 80, preserve_position=False)
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 50, "y": 60}
        )

    def test_first_replace_geometry_adopts_size_but_acknowledges_saved_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.apply_external_geometry.return_value = (500, 600, 70, 80)
        app._observer_rect = None
        app._bubble_anchor = "bundled"
        app._replace_startup_geometry_pending = True

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 50, "y": 60, "width": 70, "height": 80},
        })
        app.character.apply_external_geometry.assert_called_once_with(50, 60, 70, 80, preserve_position=True)
        self.assertFalse(app._replace_startup_geometry_pending)
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 500, "y": 600}
        )

    def test_echo_refresh_uses_anchor_relative_input_position(self):
        manager = object.__new__(BubbleManager)
        manager._echo_text = "hello"
        manager._echo_rect = (110, 220, 120, 50, "left")
        manager._echo_anchor_offset = (100, 200)
        manager._get_char_rect = lambda: (1000, 2000, 30, 40)
        manager._echo = Mock()
        manager._echo.ensure.return_value = Mock()
        manager._font_family = lambda: "Arial"
        manager._font_size = lambda: 12
        manager._theme = {"speech_fg": "#fff", "input_bg": "#000", "input_outline": "#111"}
        with (
            patch("overlay.bubble.bubble_manager.shapes.draw_speech_bubble", return_value=(120, 40)),
            patch("overlay.bubble.bubble_manager.geometry.clamp_rect", side_effect=lambda x, y, w, h, _m: (x, y)),
            patch("overlay.bubble.bubble_manager.geometry.get_monitor_work_rect", return_value=(0, 0, 3000, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.get_monitor_rect", return_value=(0, 0, 3000, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.monitor_bottom_center_pixel", return_value=(1500, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.angle_to_point", return_value=0),
        ):
            manager._render_echo()
        # Original input (110,220) was relative to (10,20), so reflow uses
        # (1100,2200) rather than stale absolute coordinates.
        manager._echo.place.assert_called_once_with(1100, 2210, 120, 40)

    def test_refresh_positions_reanchors_thought_speech_and_echo(self):
        manager = object.__new__(BubbleManager)
        calls = []
        manager._render_thought = lambda: calls.append("thought")
        manager._render_speech = lambda: calls.append("speech")
        manager._render_echo = lambda: calls.append("echo")
        manager.refresh_positions()
        self.assertEqual(calls, ["thought", "speech", "echo"])

    def test_observer_click_without_geometry_does_not_activate(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._bubble_manager = Mock()
        app._observer_rect = None
        app._bubble_anchor = "bundled"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        app.character.external_activate.assert_not_called()
        self.assertEqual(app._bubble_anchor, "bundled")

    def test_bundled_click_reselects_bundled_anchor(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock()
        app._bubble_manager = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"
        app._on_bundled_pointer_event("left_click", {})
        self.assertEqual(app._bubble_anchor, "bundled")

    def test_overlay_api_help_uses_manual_deep_link(self):
        opened = []
        open_overlay_event_api_manual(opened.append)
        self.assertEqual(opened, [OVERLAY_EVENT_API_MANUAL_URL])

    def test_custom_overlay_copy_and_quick_reference_contract(self):
        root = Path(__file__).resolve().parents[1]
        manual = (root / "installer/templates/manual/overlay-event-api.md").read_text(encoding="utf-8")
        docs = (root / "docs/overlay-event-api-v1.md").read_text(encoding="utf-8")
        settings = (root / "overlay/settings_window.py").read_text(encoding="utf-8")
        for text in (manual, docs, settings):
            self.assertNotIn("외주", text)
            self.assertNotIn("Engram의 stdin은 커스텀 오버레이로 가는", text)
        self.assertIn("## 커스텀 오버레이 → Engram — 보내야 하는 메시지", manual)
        self.assertIn("## Engram → 커스텀 오버레이 — 받는 메시지", manual)
        for name in ("overlay.hello", "overlay.geometry_changed", "pointer.action", "overlay.heartbeat", "engram.welcome", "state.snapshot", "overlay.set_position"):
            self.assertIn(name, manual)
        self.assertIn('text="?", width=3', settings)
        self.assertIn("커스텀 오버레이 적용 방법", settings)
        self.assertIn("for line in sys.stdin", manual)
        readme = (root / "README.md").read_text(encoding="utf-8")
        for expected in (
            "https://github.com/JJHbrams/engram-overlay",
            "%USERPROFILE%/.engram/overlays/<id>/",
            "Settings GUI > 오버레이",
            "`observer`는 번들 캐릭터를 유지",
            "`replace`는 번들을 대체",
            "오버레이 재시작",
            "renderer 실패 시 번들 캐릭터로 자동 복구",
            "docs/overlay-event-api-v1.md",
        ):
            self.assertIn(expected, readme)

    def test_persona_shortcut_is_tab_scoped_not_a_cli_control(self):
        source = (Path(__file__).resolve().parents[1] / "overlay/settings_window.py").read_text(encoding="utf-8")
        cli_section = source[source.index("def _build_cli_tab"):source.index("def _refresh_ollama_models")]
        self.assertNotIn("페르소나", cli_section)
        self.assertIn("grid(row=5, column=0", cli_section)
        self.assertIn("grid(row=6, column=0", cli_section)
        self.assertIn("self._persona_tip_frame", source)
        self.assertIn("<<NotebookTabChanged>>", source)
        self.assertIn("selected is self._tab_persona", source)
        self.assertIn("pack_forget", source)


if __name__ == "__main__":
    unittest.main()
