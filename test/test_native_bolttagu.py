"""Pure engine/asset evidence; real Windows presentation is a separate gate."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay.bolttagu_mapping import (
    ASSET_DIR, CLIPS, OPTIONS, STATE_POSES, frames_at, load_mapping, facing_mirrored,
)
from overlay.native_bolttagu import BolttaguAnimator, Bolttagu2dView, load_atlas


class NativeBolttaguTests(unittest.TestCase):
    def test_uncommitted_launcher_geometry_never_reapplies_offset_to_full_origin(self):
        from overlay.character import CharacterOverlay
        from unittest.mock import Mock
        character = object.__new__(CharacterOverlay)
        character.root = Mock()
        character.root.winfo_x.return_value = 30
        character.root.winfo_y.return_value = 1584
        character._img_w, character._img_h = 236, 264
        state = {'launcher_window': {'x': 20, 'y': 1836},
                 'overlay_window': {'launcher_offset_x': 102, 'launcher_offset_y': -14}}
        with patch('overlay.character.get_overlay_state', return_value=state), patch('overlay.character.bubble_geometry.get_monitor_work_rect', return_value=(0, 0, 3840, 2112)):
            for _ in range(3):
                self.assertEqual(character.capture_launcher_expand_anchor(), (20, 1836))
                self.assertEqual(character.launcher_full_target(), (30, 1584, 236, 264))

    def test_restart_size_uses_work_area_not_full_monitor_height(self):
        from overlay.character import initial_character_work_size, target_height_for_work_area
        with patch('overlay.character.bubble_geometry.get_monitor_work_rect', return_value=(0, 0, 3840, 2112)) as monitor:
            work = initial_character_work_size({'x': 50, 'y': 1816, 'width': 236, 'height': 264}, (3840, 2160))
        monitor.assert_called_once_with(168, 1948)
        self.assertEqual(work, (3840, 2112))
        self.assertEqual(target_height_for_work_area(work, .125), 264)

    def test_restart_uses_saved_negative_coordinate_monitor(self):
        from overlay.character import initial_character_work_size
        with patch('overlay.character.bubble_geometry.get_monitor_work_rect', return_value=(-1920, -1080, 0, -40)) as monitor:
            self.assertEqual(initial_character_work_size({'x': -1800, 'y': -900, 'width': 200, 'height': 240}, (3840, 2160)), (1920, 1040))
        monitor.assert_called_once_with(-1700, -780)

    def test_upstream_clock_recipe_parity_2112_samples(self):
        # Golden trace for the packaged product mapping. CI does not need an
        # installed provider package or a per-user mapping file.
        values = []
        for seed in (0, 42):
            for hint in STATE_POSES:
                for category in (None, 'read', 'write', 'execute', 'search', 'memory', 'communication', 'other'):
                    model = BolttaguAnimator(intro=None, seed=seed)
                    model.apply_hint(hint, 0, category)
                    values.extend(model.resolve(stamp) for stamp in (0, 49, 100, 250, 550, 999, 1000, 2500, 2710, 6000, 12345))
        self.assertEqual(len(values), 2112)
        self.assertEqual(hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest(),
                         '0111831341ce25ccafb13b75c501bc67fb9e304ec920e0661b16da90a7970088')

    def test_every_bundled_recipe_is_drawable(self):
        sheets, cell = load_atlas()
        self.assertEqual(len(list(ASSET_DIR.glob('*.png'))), 30)
        for option in OPTIONS.values():
            for stamp in (0, 49, 100, 400, 999, 2200, 5600):
                for sheet, index in frames_at(option, stamp, 42):
                    self.assertEqual(sheets[sheet][index].size, cell)

    def test_editor_preview_composes_the_native_atlas_recipe(self):
        from overlay.bolttagu_editor import compose_preview_frame
        sheets, _ = load_atlas()
        view = Bolttagu2dView(launcher_managed=True)
        for option in ('idle', 'trickcal-idle', 'writing', 'trickcal-success'):
            for stamp in (0, 100, 550, 2500):
                self.assertEqual(
                    compose_preview_frame(sheets, option, stamp).tobytes(),
                    view.compose(frames_at(OPTIONS[option], stamp), False).tobytes(),
                )

    def test_idle_has_independent_blink_and_steam(self):
        model = BolttaguAnimator(intro=None)
        self.assertEqual(model.resolve(0), (('trickcal-idle', 0), ('trickcal-steam', 0)))
        self.assertEqual(model.resolve(100), (('trickcal-idle', 0), ('trickcal-steam', 1)))
        self.assertEqual(model.resolve(2500)[0], ('trickcal-idle', 1))
        self.assertEqual(model.resolve(2550)[0], ('trickcal-idle', 2))
        self.assertEqual(model.resolve(2710)[0], ('trickcal-idle', 0))

    def test_hint_category_success_and_lifecycle(self):
        model = BolttaguAnimator(intro=None)
        model.apply_hint('generating', 0, 'write')
        self.assertEqual(model.resolve(0), (('trickcal-writing', 0),))
        model.apply_hint('memory', 1, 'read')
        self.assertEqual(model.resolve(1), (('trickcal-searching', 0),))
        model.apply_hint('success', 2)
        self.assertEqual(model.resolve(2), (('success', 0),))
        self.assertEqual(model.resolve(1002)[0][0], 'trickcal-idle')
        duration = model.play_lifecycle('exit', 2000, hold_last=True)
        self.assertEqual(duration, 700)
        self.assertEqual(model.resolve(9000), (('exit', 2),))

    def test_explicit_mapping_only_no_external_user_lookup(self):
        with patch('pathlib.Path.home', side_effect=AssertionError('no home lookup')):
            self.assertEqual(load_mapping().hints['memory'], 'trickcal-searching')

    def test_corrupt_and_escaping_atlas_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'atlas.json').write_text(json.dumps({'cell': [20, 20], 'sheets': {'../escape.png': [0]}}))
            with self.assertRaises(ValueError):
                load_atlas(path)

    def test_view_has_no_window_or_timer_and_redraws_only_changes(self):
        with patch.object(Bolttagu2dView, '_now_ms', return_value=0):
            view = Bolttagu2dView(launcher_managed=True, face_pointer=False)
            self.assertIsNone(view.canvas)
            image = view.render_frame(0, 0)
            self.assertEqual(image.size, view.cell)
            self.assertIsNone(view.render_frame(0, 0))
            view.resize(0, 300)
            self.assertIsNotNone(view.render_frame(0, 0))

    def test_pointer_deadzone_and_exit_orientation(self):
        self.assertFalse(facing_mirrored(50, 0, 100, current=False))
        self.assertTrue(facing_mirrored(90, 0, 100, current=False))
        view = Bolttagu2dView(launcher_managed=True)
        view.mirrored = True
        view.begin_exit()
        view.render_frame(-9000, 0)
        self.assertTrue(view.mirrored)

    def test_damaged_selected_mapping_emits_sanitized_warning_and_defaults(self):
        from overlay.character import CharacterOverlay
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mapping.json'
            path.write_text('{"hints":{"idle":"private-fixture-content"}}')
            cfg = {'overlay': {'character': {'bolttagu': {'mapping_path': str(path)}}}}
            with self.assertLogs('overlay.character', level='WARNING') as captured:
                view = CharacterOverlay._create_native_view(cfg, SimpleNamespace(native_enabled=True))
            self.assertIsNotNone(view)
            self.assertEqual(view.mapping.hints['idle'], 'trickcal-idle')
            self.assertEqual(len(captured.output), 1)
            self.assertNotIn('private-fixture-content', captured.output[0])


if __name__ == '__main__':
    unittest.main()
