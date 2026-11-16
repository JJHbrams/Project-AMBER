import ast
import json
import unittest
from pathlib import Path
import tempfile

from overlay.native_bolttagu import BolttaguAnimator, load_atlas
from overlay.native_mnema import ASSET_DIR, MNEMA_STATE_STRIPS, SPRITE_PREFIX, MnemaView
from overlay.native_engram_icon import ICON_CLIPS, ICON_OPTIONS
from overlay.mnema_mapping import CATEGORIES, HINTS, LIFECYCLE, ONESHOTS, load_mapping, validate_mapping_document
from overlay.mnema_mapping import sprite_map
from overlay.bolttagu_editor import sparse_mapping_document
from core.install.native_bolttagu import store_mapping
from overlay.character import CharacterOverlay, _CharacterProfile


class NativeMnemaTests(unittest.TestCase):
    def test_mnema_atlas_uses_the_native_icon_state_contract(self):
        sheets, cell = load_atlas(ASSET_DIR, sprite_prefix=SPRITE_PREFIX, required_options=ICON_OPTIONS)
        self.assertEqual(cell, (270, 302))
        self.assertEqual(set(sheets), set(MNEMA_STATE_STRIPS))
        self.assertEqual(len(sheets["steam"]), 24)
        self.assertEqual(len(sheets["alert"]), 1)

    def test_mnema_preserves_event_and_lifecycle_semantics(self):
        model = BolttaguAnimator(intro=None, hints=HINTS, categories=CATEGORIES, oneshots=ONESHOTS, clips=ICON_CLIPS, options=ICON_OPTIONS)
        model.apply_hint("generating", 0, "write")
        self.assertEqual(model.resolve(0), (("writing", 0),))
        self.assertEqual(model.play_lifecycle(LIFECYCLE["show"], 100), 720)

    def test_mnema_defaults_assign_distinct_event_meanings(self):
        model = BolttaguAnimator(intro=None, hints=HINTS, categories=CATEGORIES, oneshots=ONESHOTS, clips=ICON_CLIPS, options=ICON_OPTIONS)
        for hint, expected in (("thought", "waiting"), ("error", "wondering"), ("provider_error", "error")):
            model.apply_hint(hint, 0)
            self.assertEqual(model.resolve(0)[0][0], expected)
        model.apply_hint("generating", 1, "execute")
        self.assertEqual(model.resolve(1)[0][0], "executing")
        self.assertEqual(LIFECYCLE["hide"], "exit")

    def test_mnema_mapping_validates_its_own_atlas_and_rejects_other_character(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mnema.json"
            path.write_text('{"version":1,"overlay_id":"mnema-2d","categories":{"execute":"waiting"}}', encoding="utf-8")
            mapping = load_mapping(path)
            self.assertEqual(mapping.categories["execute"], "waiting")
            self.assertEqual(mapping.hints["thought"], "waiting")
            with self.assertRaisesRegex(ValueError, "다른 캐릭터"):
                validate_mapping_document({"version": 1, "overlay_id": "bolttagu-2d"})

    def test_mnema_view_resolves_selected_mapping_without_doctor_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mnema.json"
            path.write_text('{"version":1,"overlay_id":"mnema-2d","categories":{"execute":"executing"}}', encoding="utf-8")
            view = MnemaView(launcher_managed=True, mapping_path=path)
            view.animator.apply_hint("generating", 0, "execute")
            self.assertEqual(view.animator.resolve(0)[0][0], "executing")

    def test_mnema_editor_document_persists_in_its_own_mapping_directory(self):
        schema = sprite_map()
        selections = {
            (section.key, row.key): row.default[0] if row.default else None
            for section in schema.sections for row in section.rows if not section.hidden
        }
        selections[("categories", "execute")] = "waiting"
        document = sparse_mapping_document(schema, {"version": 1}, selections)
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory) / "native-mnema" / "mappings"
            path = store_mapping(document, location, validator=validate_mapping_document)
            self.assertEqual(path.parent, location)
            self.assertEqual(load_mapping(path).categories["execute"], "waiting")

    def test_mnema_mapping_path_is_part_of_the_config_reload_watch_set(self):
        path = Path("C:/temporary/mnema-mapping.json")
        cfg = {"overlay": {"character": {"source_mode": "native_mnema", "mnema": {"mapping_path": str(path)}}}}
        view = CharacterOverlay.__new__(CharacterOverlay)
        view._cfg = cfg
        self.assertIn(path, view._character_watch_paths_for(_CharacterProfile(cfg)))

    def test_native_mnema_mode_is_enabled_without_changing_other_sources(self):
        profile = _CharacterProfile({"overlay": {"character": {"source_mode": "native_mnema"}}})
        self.assertTrue(profile.native_enabled)
        self.assertEqual(profile.source_mode, "native_mnema")

    def test_validation_records_common_feet_and_unclipped_frame_bounds(self):
        validation = json.loads((ASSET_DIR / "asset-validation.json").read_text(encoding="utf-8"))
        atlas = json.loads((ASSET_DIR / "atlas.json").read_text(encoding="utf-8"))
        self.assertEqual((validation["center_x"], validation["feet_y"]), (135, 270))
        self.assertEqual(atlas["feetAnchor"], [135, 270])
        for state in validation["states"].values():
            for frame in state["frames"]:
                left, top, right, bottom = frame["bbox"]
                self.assertGreater(left, 0)
                self.assertGreater(top, 0)
                self.assertLess(right, 270)
                self.assertLess(bottom, 302)
                self.assertEqual(frame["feet_anchor_y"], 270)

    def test_frozen_package_collector_includes_mnema_assets(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "engram-overlay.spec").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_collect_character_datas")
        namespace = {"Path": Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<asset-collector>", "exec"), namespace)
        collected = {Path(source).resolve() for source, _ in namespace["_collect_character_datas"]()}
        self.assertTrue(set(ASSET_DIR.iterdir()).issubset(collected))


if __name__ == "__main__":
    unittest.main()
