import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from overlay import config


class OverlayConfigLegacyCharacterTests(unittest.TestCase):
    def _load(self, user_data: dict) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            default_path = root / "overlay.yaml"
            user_path = root / "overlay.user.yaml"
            state_path = root / "overlay.state.yaml"
            default_path.write_text(
                "overlay:\n  character:\n    name: engram\n    source_mode: sprite_grid\n",
                encoding="utf-8",
            )
            user_path.write_text(yaml.safe_dump(user_data), encoding="utf-8")
            with (
                patch.object(config, "_USER_CONFIG_PATH", user_path),
                patch.object(config, "_STATE_PATH", state_path),
                patch.object(config, "resolve_editable_overlay_path", return_value=default_path),
            ):
                return config.load_cfg(strict=True)

    def test_legacy_custom_static_override_beats_new_sprite_default(self):
        cfg = self._load({"overlay": {"character": {"name": "C:/characters/custom.png"}}})

        self.assertEqual(cfg["overlay"]["character"]["name"], "C:/characters/custom.png")
        self.assertEqual(cfg["overlay"]["character"]["source_mode"], "static")

    def test_legacy_custom_sequence_override_beats_new_sprite_default(self):
        cfg = self._load({"overlay": {"character": {"name": "C:/characters/frames"}}})

        self.assertEqual(cfg["overlay"]["character"]["source_mode"], "sequence")

    def test_legacy_default_name_receives_new_sprite_default(self):
        cfg = self._load({"overlay": {"character": {"name": "engram"}}})

        self.assertEqual(cfg["overlay"]["character"]["source_mode"], "sprite_grid")

    def test_explicit_source_mode_is_never_migrated(self):
        cfg = self._load(
            {"overlay": {"character": {"name": "C:/characters/custom.png", "source_mode": "sequence"}}}
        )

        self.assertEqual(cfg["overlay"]["character"]["source_mode"], "sequence")


if __name__ == "__main__":
    unittest.main()
