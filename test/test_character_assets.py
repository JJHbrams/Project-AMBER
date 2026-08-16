import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay.character import _CharacterProfile
from overlay.character_assets import CharacterEffectAsset, CharacterSetResolution, resolve_character_set, resolve_reaction_pack


def _write_pack(root: Path, manifest: str) -> None:
    (root / "effects").mkdir(parents=True, exist_ok=True)
    (root / "character.png").write_bytes(b"png")
    (root / "effects" / "idle.png").write_bytes(b"png")
    (root / "effects" / "click.png").write_bytes(b"png")
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")


_MANIFEST = """\
schema_version: 1
id: engram
display_name: Engram
character: character.png
effects:
  idle: {asset: effects/idle.png, thickness_px: 2}
  click: {asset: effects/click.png, thickness_px: 3}
"""

_REACTION_MANIFEST = """\
schema_version: 1
id: engram
sprite_sheet: states.png
chroma_key: "#00FF00"
grid: {columns: 6, rows: 4, cell_width: 434, cell_height: 408}
mapping: {thought: 1, success: 2}
"""


def _write_reaction_pack(root: Path, manifest: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "states.png").write_bytes(b"png")
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")


class CharacterAssetResolverTests(unittest.TestCase):
    def test_user_pack_wins_over_bundled_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_root, bundled_root = root / "user", root / "bundled"
            _write_pack(user_root / "engram", _MANIFEST)
            _write_pack(bundled_root / "engram", _MANIFEST)
            with patch("overlay.character_assets.USER_CHARACTER_SETS_DIR", user_root), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=bundled_root / "engram"
            ):
                result = resolve_character_set("engram")

            self.assertEqual(result.source, "user")
            self.assertEqual(result.base_image, user_root / "engram" / "character.png")

    def test_manifest_rejects_absolute_and_traversal_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "bundled" / "engram"
            _write_pack(bad, _MANIFEST.replace("character.png", "../outside.png"))
            with patch("overlay.character_assets.USER_CHARACTER_SETS_DIR", root / "user"), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=bad
            ):
                self.assertEqual(resolve_character_set("engram").source, "disabled")

            _write_pack(bad, _MANIFEST.replace("character.png", "C:/outside.png"))
            with patch("overlay.character_assets.USER_CHARACTER_SETS_DIR", root / "user"), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=bad
            ):
                self.assertEqual(resolve_character_set("engram").source, "disabled")

    def test_legacy_absolute_base_and_effect_fallback_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, legacy_click = root / "custom.png", root / "click.png"
            base.write_bytes(b"png")
            legacy_click.write_bytes(b"png")
            manifest_idle = root / "manifest-idle.png"
            manifest_idle.write_bytes(b"png")
            resolution = CharacterSetResolution(
                source="bundled",
                base_image=root / "manifest-base.png",
                idle=CharacterEffectAsset(manifest_idle, 4),
            )
            cfg = {
                "overlay": {
                    "character": {
                        "name": str(base),
                        "set": "engram",
                        "effects": {
                            "enabled": True,
                            "idle_thickness_px": 2,
                            "click_thickness_px": 6,
                            "click_asset": str(legacy_click),
                        },
                    }
                }
            }
            with patch("overlay.character.resolve_character_set", return_value=resolution):
                profile = _CharacterProfile(cfg)

        self.assertEqual(profile.default_frame, base)
        self.assertEqual(profile.effects_idle_asset, manifest_idle)
        self.assertEqual(profile.effects_idle_thickness_px, 4)
        self.assertEqual(profile.effects_click_asset, legacy_click)
        self.assertEqual(profile.effects_click_thickness_px, 6)

    def test_reaction_user_pack_wins_and_validates_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_root, bundled_root = root / "user", root / "bundled"
            _write_reaction_pack(user_root / "engram", _REACTION_MANIFEST)
            _write_reaction_pack(bundled_root / "engram", _REACTION_MANIFEST)
            with patch("overlay.character_assets.USER_REACTION_PACKS_DIR", user_root), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=bundled_root / "engram"
            ):
                result = resolve_reaction_pack("engram")

        self.assertEqual(result.source, "user")
        self.assertEqual(result.mapping, {"thought": 1, "success": 2})

    def test_reaction_manifest_rejects_out_of_range_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "bundled" / "engram"
            _write_reaction_pack(bad, _REACTION_MANIFEST.replace("success: 2", "success: 24"))
            with patch("overlay.character_assets.USER_REACTION_PACKS_DIR", root / "user"), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=bad
            ):
                self.assertEqual(resolve_reaction_pack("engram").source, "disabled")

    def test_reaction_manifest_normalizes_legacy_effect_aliases(self):
        manifest = _REACTION_MANIFEST + "states: {hover: {frames: [1], transform: hover, vfx: idle}, click: {frames: [2], transform: click, vfx: click}}\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_reaction_pack(root / "engram", manifest)
            with patch("overlay.character_assets.USER_REACTION_PACKS_DIR", root), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=root / "missing"
            ):
                result = resolve_reaction_pack("engram")

        self.assertEqual(result.states["hover"]["transform"], "hflip_squash")
        self.assertEqual(result.states["hover"]["vfx"], "twinkle")
        self.assertEqual(result.states["click"]["transform"], "none")
        self.assertEqual(result.states["click"]["vfx"], "sparkle_burst")


if __name__ == "__main__":
    unittest.main()
