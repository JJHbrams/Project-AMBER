import re
import unittest
from pathlib import Path

from core.install.runtime_contract import evaluate_runtime_contract


ROOT = Path(__file__).resolve().parents[1]


class RuntimeContractTests(unittest.TestCase):
    def test_source_contract_uses_checkout_entry_and_required_resources(self):
        result = evaluate_runtime_contract()

        self.assertEqual(result["contract_version"], 1)
        self.assertEqual(result["runtime"], "source")
        # Pin the shape against the repo's own VERSION rather than a literal:
        # a hardcoded release number silently rots one bump after it is written.
        expected = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(result["version"], rf"^{re.escape(expected)}\.\d+$")
        self.assertIn(
            result["version_build_source"],
            {"git", "fallback", "SEMVER4_BUILD", "GITHUB_RUN_NUMBER", "CI_PIPELINE_IID", "BUILD_NUMBER"},
        )
        self.assertEqual(Path(result["source_root"]).resolve(), ROOT.resolve())
        self.assertEqual(result["entrypoint"], "engram_overlay_entry.py")
        self.assertGreater(result["stm_port"], 0)
        self.assertGreater(result["mcp_port"], 0)
        self.assertTrue(Path(result["resources"]["config/overlay.yaml"]).is_file())
        self.assertTrue(Path(result["resources"]["resource/icon.png"]).is_file())


if __name__ == "__main__":
    unittest.main()
