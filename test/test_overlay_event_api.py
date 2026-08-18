import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from overlay.character import CharacterOverlay
from overlay.event_api import DISPLAY_HINTS, OverlayEventPublisher, event_for_bubble, tool_category
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
        proc = Mock()
        proc.stdout = io.StringIO(
            '{"type":"overlay.hello","payload":{"supported_schema_versions":[1]}}\n'
        )
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
