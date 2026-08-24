import unittest
from pathlib import Path

from core.install.runtime_contract import evaluate_runtime_contract


ROOT = Path(__file__).resolve().parents[1]


class RuntimeContractTests(unittest.TestCase):
    def test_source_contract_uses_checkout_entry_and_required_resources(self):
        result = evaluate_runtime_contract()

        self.assertEqual(result["contract_version"], 1)
        self.assertEqual(result["runtime"], "source")
        self.assertRegex(result["version"], r"^1\.5\.5\.\d+$")
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
