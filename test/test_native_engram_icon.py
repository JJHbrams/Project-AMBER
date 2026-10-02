import ast
import unittest
from pathlib import Path

from overlay.native_bolttagu import BolttaguAnimator, load_atlas
from overlay.native_engram_icon import (
    ASSET_DIR,
    ENGRAM_ICON_STATE_STRIPS,
    ICON_CATEGORIES,
    ICON_CLIPS,
    ICON_HINTS,
    ICON_LIFECYCLE,
    ICON_ONESHOTS,
    ICON_OPTIONS,
    SPRITE_PREFIX,
)


class NativeEngramIconTests(unittest.TestCase):
    def test_all_trickcal_strip_semantics_have_icon_equivalents(self):
        self.assertEqual(len(ENGRAM_ICON_STATE_STRIPS), 15)
        required_sheets = {
            layer.sheet for option in ICON_OPTIONS.values() for layer in option.layers
        }
        self.assertEqual(set(ENGRAM_ICON_STATE_STRIPS), required_sheets)
        self.assertEqual(ICON_CLIPS["alert"].cells, (0,))
        steam = next(layer for layer in ICON_OPTIONS["idle"].layers if layer.sheet == "steam")
        self.assertEqual(len(steam.cells), 24)

    def test_native_icon_preserves_public_event_and_lifecycle_mapping(self):
        model = BolttaguAnimator(
            intro=None,
            hints=ICON_HINTS,
            categories=ICON_CATEGORIES,
            oneshots=ICON_ONESHOTS,
            clips=ICON_CLIPS,
            options=ICON_OPTIONS,
        )
        model.apply_hint("generating", 0, "write")
        self.assertEqual(model.resolve(0), (("writing", 0),))
        model.apply_hint("success", 2)
        self.assertEqual(model.resolve(2), (("success", 0),))
        self.assertEqual(model.play_lifecycle(ICON_LIFECYCLE["show"], 100), 720)

    def test_bundled_icon_atlas_loads_every_required_strip(self):
        sheets, cell = load_atlas(
            ASSET_DIR,
            sprite_prefix=SPRITE_PREFIX,
            required_options=ICON_OPTIONS,
        )

        self.assertEqual(cell, (270, 302))
        self.assertEqual(set(sheets), set(ENGRAM_ICON_STATE_STRIPS))
        self.assertEqual(len(sheets["alert"]), 1)
        self.assertEqual(len(sheets["steam"]), 24)

    def test_frozen_package_collector_includes_every_icon_asset(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "engram-overlay.spec").read_text(encoding="utf-8"))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_collect_character_datas"
        )
        namespace = {"Path": Path}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<asset-collector>", "exec"), namespace)
        collected = {Path(source).resolve() for source, _ in namespace["_collect_character_datas"]()}

        self.assertTrue(set(ASSET_DIR.iterdir()).issubset(collected))


if __name__ == "__main__":
    unittest.main()
