import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CopilotBootstrapInstallTests(unittest.TestCase):
    def test_full_installer_registers_global_instruction_directory(self):
        env_module = (ROOT / "installer" / "modules" / "08_env.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")', env_module)

    def test_frozen_installer_deploys_and_registers_copilot_protocol(self):
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")
        self.assertIn("config\\clients\\copilot.md", configure)
        self.assertIn('SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")', configure)
        self.assertIn('Source: "..\\config\\clients\\copilot.md"', iss)

    def test_wrapper_keeps_bootstrap_for_interactive_options(self):
        shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('set `"SKIP_BOOTSTRAP=0`"', shims)
        self.assertIn(
            'else ($EngramCopilotCmd -i `"!ENGRAM_BOOTSTRAP!`" !ARGS!)',
            shims,
        )


if __name__ == "__main__":
    unittest.main()
