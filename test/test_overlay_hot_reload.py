import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from overlay.character import CharacterOverlay, SpriteStateMachine, fingerprint_paths
from overlay import config


class EditableOverlayResolverTests(unittest.TestCase):
    def test_verified_checkout_beats_frozen_bundle_for_overlay_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Project_Engram"
            (root / "overlay").mkdir(parents=True)
            (root / "config").mkdir()
            (root / "INSTALL.ps1").write_text("# marker", encoding="utf-8")
            (root / "overlay" / "config.py").write_text("# marker", encoding="utf-8")
            source = root / "config" / "overlay.yaml"
            source.write_text("overlay: {}", encoding="utf-8")
            install = root / "dist" / "engram-overlay"
            install.mkdir(parents=True)
            bundle = Path(temporary) / "bundle"
            (bundle / "config").mkdir(parents=True)
            bundled = bundle / "config" / "overlay.yaml"
            bundled.write_text("overlay: {bundled: true}", encoding="utf-8")
            with patch.object(config.sys, "frozen", True, create=True), patch.object(
                config, "_get_base_dir", return_value=install
            ), patch.object(config, "_get_bundle_dir", return_value=bundle):
                self.assertEqual(config.resolve_editable_overlay_path("config/overlay.yaml"), source)

    def test_unverified_copied_install_stays_bundle_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "copy" / "engram-overlay"
            install.mkdir(parents=True)
            bundle = root / "bundle"
            (bundle / "config").mkdir(parents=True)
            bundled = bundle / "config" / "overlay.yaml"
            bundled.write_text("overlay: {}", encoding="utf-8")
            with patch.object(config.sys, "frozen", True, create=True), patch.object(
                config, "_get_base_dir", return_value=install
            ), patch.object(config, "_get_bundle_dir", return_value=bundle):
                self.assertEqual(config.resolve_editable_overlay_path("config/overlay.yaml"), bundled)


class WatchFingerprintTests(unittest.TestCase):
    def test_fingerprint_tracks_mtime_and_size_and_is_sorted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "a.yaml", root / "b.yaml"
            first.write_text("a", encoding="utf-8")
            second.write_text("bb", encoding="utf-8")
            before = fingerprint_paths({second, first})
            first.write_text("changed", encoding="utf-8")
            after = fingerprint_paths({second, first})
        self.assertEqual([entry[0] for entry in before], sorted(entry[0] for entry in before))
        self.assertNotEqual(before, after)

    def test_poller_debounces_a_signature_before_reloading(self):
        class Root:
            def winfo_exists(self):
                return True

            def after(self, delay, callback):
                return "after-id"

        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay.root = Root()
        overlay._watch_after_id = None
        overlay._watch_signature = (("old", (1, 1)),)
        overlay._watch_pending_signature = None
        overlay._watch_pending_at = 0.0
        overlay._character_watch_paths = lambda: set()
        overlay.reload_config = Mock(return_value=True)
        changed = (("new", (2, 2)),)
        with patch("overlay.character.fingerprint_paths", return_value=changed), patch(
            "overlay.character.time.monotonic", side_effect=(10.0, 10.31)
        ):
            overlay._poll_config_watch()
            overlay._poll_config_watch()
        overlay.reload_config.assert_called_once_with()


class CharacterReloadTests(unittest.TestCase):
    def _overlay(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._cfg = {"overlay": {"character": {}}}
        overlay._profile = SimpleNamespace(default_frame=Path("old.png"))
        overlay._sprite_model = SpriteStateMachine(set(), started_ms=1)
        overlay._current_source = Path("old.png")
        overlay._work_size = (1000, 800)
        overlay._img_h = 100
        overlay._flip_h = False
        overlay._sprite_cache = {"old": object()}
        overlay._sprite_choices = {("idle", 1, 0): 0}
        overlay._sprite_shuffle_orders = {("idle", 1, 0): (0,)}
        overlay._sprite_selection_epoch = 1
        overlay._sequence_queue = []
        overlay._click_started_ms = 0
        overlay._character_watch_paths = lambda: set()
        overlay._character_watch_paths_for = lambda profile: set()
        overlay._validate_yaml_mappings = lambda paths: None
        overlay._decode_image = lambda path: None
        overlay._load_sprite_sheet_for = lambda profile: None
        overlay._effect_images_for = lambda profile: {}
        overlay._resize_window_to_image = lambda old_height: None
        overlay._schedule_animation_in = lambda delay: None
        overlay._watch_signature = ()
        overlay._watch_pending_signature = None
        return overlay

    def test_static_source_reload_uses_new_default_not_old_current_source(self):
        overlay = self._overlay()
        new_profile = SimpleNamespace(default_frame=Path("new.png"), sprite_enabled=False, reaction_pack=SimpleNamespace(states={}))
        loaded = []
        overlay._load_image = lambda work_size, source_path: loaded.append(source_path)
        with patch("overlay.character.load_cfg", return_value={"overlay": {"flip_horizontal": False}}), patch(
            "overlay.character._CharacterProfile", return_value=new_profile
        ):
            self.assertTrue(overlay.reload_config())
        self.assertEqual(loaded, [Path("new.png")])
        self.assertEqual(overlay._sprite_cache, {})

    def test_valid_reload_swaps_assets_preserves_work_area_and_resets_caches(self):
        overlay = self._overlay()
        overlay._sprite_model = SpriteStateMachine({"idle", "hover"}, state="hover", work_state="idle", hovered=True, started_ms=7)
        states = {"idle": {"frames": (0,)}, "hover": {"frames": (0,)}}
        new_profile = SimpleNamespace(default_frame=Path("new.png"), sprite_enabled=True, reaction_pack=SimpleNamespace(states=states))
        sheet, effects = object(), {"idle": object()}
        loaded, geometry = [], []
        overlay._load_sprite_sheet_for = lambda profile: sheet
        overlay._effect_images_for = lambda profile: effects
        overlay._load_image = lambda work_size, source_path: loaded.append((work_size, source_path))
        overlay._resize_window_to_image = lambda old_height: geometry.append(old_height)
        with patch("overlay.character.load_cfg", return_value={"overlay": {"flip_horizontal": True}}), patch(
            "overlay.character._CharacterProfile", return_value=new_profile
        ):
            self.assertTrue(overlay.reload_config())
        self.assertIs(overlay._profile, new_profile)
        self.assertIs(overlay._sprite_sheet, sheet)
        self.assertIs(overlay._effect_images, effects)
        self.assertEqual(overlay._sprite_cache, {})
        self.assertEqual(loaded, [((1000, 800), Path("new.png"))])
        self.assertEqual(geometry, [100])
        self.assertEqual(overlay._work_size, (1000, 800))
        self.assertEqual(overlay._sprite_model.state, "hover")
        self.assertTrue(overlay._flip_h)

    def test_invalid_current_manifest_keeps_last_good_profile_and_caches(self):
        overlay = self._overlay()
        old = overlay._profile
        cache = overlay._sprite_cache
        with tempfile.TemporaryDirectory() as temporary:
            invalid = Path(temporary) / "manifest.yaml"
            invalid.write_text("states: [", encoding="utf-8")
            overlay._character_watch_paths = lambda: {invalid}
            overlay._validate_yaml_mappings = CharacterOverlay._validate_yaml_mappings
            new_profile = SimpleNamespace(default_frame=Path("new.png"), sprite_enabled=False, reaction_pack=SimpleNamespace(states={}))
            with patch("overlay.character.load_cfg", return_value={"overlay": {}}), patch(
                "overlay.character._CharacterProfile", return_value=new_profile
            ):
                self.assertFalse(overlay.reload_config())
        self.assertIs(overlay._profile, old)
        self.assertIs(overlay._sprite_cache, cache)

    def test_enabled_sprite_that_fails_to_resolve_is_rejected(self):
        overlay = self._overlay()
        old = overlay._profile
        rejected = SimpleNamespace(default_frame=Path("new.png"), sprite_enabled=False, reaction_pack=SimpleNamespace(states={}))
        cfg = {"overlay": {"character": {"source_mode": "sprite_grid", "reactions": {"enabled": True}}}}
        with patch("overlay.character.load_cfg", return_value=cfg), patch(
            "overlay.character._CharacterProfile", return_value=rejected
        ):
            self.assertFalse(overlay.reload_config())
        self.assertIs(overlay._profile, old)

    def test_configured_user_manifest_is_watched_even_after_bundled_fallback(self):
        overlay = self._overlay()
        profile = SimpleNamespace(
            set_id="engram",
            reactions_cfg={"pack": "engram"},
            default_frame=Path("base.png"),
            effects_idle_asset=None,
            effects_click_asset=None,
            set_resolution=SimpleNamespace(base_image=None),
            reaction_pack=SimpleNamespace(sprite_sheet=None),
        )
        paths = CharacterOverlay._character_watch_paths_for(overlay, profile)
        self.assertIn(Path.home() / ".engram" / "character" / "sets" / "engram" / "manifest.yaml", paths)
        self.assertIn(Path.home() / ".engram" / "character" / "reactions" / "engram" / "manifest.yaml", paths)
