from pathlib import Path
import tempfile
import unittest

from overlay.external_renderer import apply_renderer_selection, discover_renderers, load_renderer_manifest


class ExternalRendererTests(unittest.TestCase):
    def write_manifest(self, home: Path, renderer_id: str, text: str, executable=True):
        folder = home / ".engram" / "overlays" / renderer_id
        folder.mkdir(parents=True)
        if executable:
            (folder / "renderer.exe").write_text("", encoding="utf-8")
        path = folder / "manifest.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_discovery_canonicalizes_relative_command_and_defaults_observer(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.write_manifest(home, "demo", "schema_version: 1\nid: demo\nname: Demo\ncommand: [renderer.exe, --jsonl]\n")
            renderers, diagnostics = discover_renderers(home)
            self.assertEqual([], diagnostics)
            self.assertEqual(["demo"], [item.id for item in renderers])
            self.assertEqual(("observer",), renderers[0].supported_modes)
            self.assertTrue(Path(renderers[0].command[0]).is_absolute())

    def test_discovery_order_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for renderer_id in ("zeta", "Alpha"):
                self.write_manifest(home, renderer_id, f"schema_version: 1\nid: {renderer_id}\nname: {renderer_id}\ncommand: [renderer.exe]\n")
            renderers, diagnostics = discover_renderers(home)
            self.assertEqual([], diagnostics)
            self.assertEqual(["Alpha", "zeta"], [renderer.id for renderer in renderers])

    def test_bad_manifest_is_diagnosed_and_not_selectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.write_manifest(home, "demo", "schema_version: 2\nid: demo\nname: Demo\ncommand: [renderer.exe]\n")
            renderers, diagnostics = discover_renderers(home)
            self.assertEqual([], renderers)
            self.assertIn("schema_version", diagnostics[0].reason)

    def test_command_shape_and_identity_validation(self):
        cases = {
            "scalar": "command: renderer.exe\n",
            "missing": "",
            "empty": "command: []\n",
            "non_string": "command: [renderer.exe, 2]\n",
            "mismatch": "id: other\ncommand: [renderer.exe]\n",
            "unsafe": "id: ../bad\ncommand: [renderer.exe]\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for case, extra in cases.items():
                manifest = self.write_manifest(home, case, f"schema_version: 1\nid: {case}\nname: {case}\n{extra}")
                with self.subTest(case=case):
                    with self.assertRaises(ValueError):
                        load_renderer_manifest(manifest)

    def test_supported_modes_and_missing_executable_are_rejected(self):
        cases = {
            "empty": "supported_modes: []",
            "invalid": "supported_modes: [observer, invalid]",
            "duplicate": "supported_modes: [observer, OBSERVER]",
            "non_string": "supported_modes: [observer, 2]",
        }
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for case, modes in cases.items():
                manifest = self.write_manifest(home, case, f"schema_version: 1\nid: {case}\nname: {case}\ncommand: [renderer.exe]\n{modes}\n")
                with self.subTest(case=case):
                    with self.assertRaises(ValueError):
                        load_renderer_manifest(manifest)
            absent = self.write_manifest(home, "absent", "schema_version: 1\nid: absent\nname: Absent\ncommand: [renderer.exe]\n", executable=False)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                load_renderer_manifest(absent)

    def test_relative_escape_and_selection_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            manifest = self.write_manifest(home, "demo", "schema_version: 1\nid: demo\nname: Demo\ncommand: [../escape.exe]\n")
            with self.assertRaisesRegex(ValueError, "within manifest"):
                load_renderer_manifest(manifest)
            good = self.write_manifest(home, "good", "schema_version: 1\nid: good\nname: Good\ncommand: [renderer.exe]\nsupported_modes: [observer, replace]\n")
            renderer = load_renderer_manifest(good)
            cfg = {"overlay": {"external_renderer": {"mode": "observer", "command": ["old"]}, "keep": "yes"}, "other": {"keep": True}}
            self.assertFalse(apply_renderer_selection(cfg, renderer, "unsupported"))
            self.assertEqual(["old"], cfg["overlay"]["external_renderer"]["command"])
            self.assertTrue(apply_renderer_selection(cfg, renderer, "replace"))
            self.assertEqual({"mode": "replace", "command": list(renderer.command)}, cfg["overlay"]["external_renderer"])
            self.assertTrue(apply_renderer_selection(cfg, None))
            self.assertNotIn("external_renderer", cfg["overlay"])
            self.assertEqual("yes", cfg["overlay"]["keep"])
            self.assertTrue(cfg["other"]["keep"])

    def test_invalid_mode_is_atomic_when_overlay_is_missing_or_not_a_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            manifest = self.write_manifest(home, "good", "schema_version: 1\nid: good\nname: Good\ncommand: [renderer.exe]\nsupported_modes: [observer]\n")
            renderer = load_renderer_manifest(manifest)
            for cfg in ({"other": "keep"}, {"overlay": "legacy", "other": "keep"}):
                before = dict(cfg)
                with self.subTest(cfg=cfg):
                    self.assertFalse(apply_renderer_selection(cfg, renderer, "replace"))
                    self.assertEqual(before, cfg)
