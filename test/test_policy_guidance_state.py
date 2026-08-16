from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.integrations.policy_guidance_state import sync_policy_guidance_disabled_marker
from overlay.settings_window import (
    _POLICY_LEVEL_DISPLAY_TO_VALUE,
    _POLICY_LEVEL_OPTIONS,
)


class PolicyGuidanceStateTests(unittest.TestCase):
    def test_settings_exposes_three_policy_levels(self):
        self.assertEqual(len(_POLICY_LEVEL_OPTIONS), 3)
        self.assertEqual(
            set(_POLICY_LEVEL_DISPLAY_TO_VALUE.values()),
            {"off", "warn", "enforce_agents"},
        )

    def test_disabled_marker_is_created_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / ".engram" / "policy-guidance.disabled"
            with patch(
                "core.integrations.policy_guidance_state.POLICY_GUIDANCE_DISABLED_PATH",
                marker,
            ):
                disabled = sync_policy_guidance_disabled_marker(False)
                self.assertTrue(disabled["ok"])
                self.assertTrue(marker.exists())

                enabled = sync_policy_guidance_disabled_marker(True)
                self.assertTrue(enabled["ok"])
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
