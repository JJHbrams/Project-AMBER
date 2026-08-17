import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "installer" / "modules" / "character_source.ps1"
ENV_MODULE = ROOT / "installer" / "modules" / "08_env.ps1"


class InstallerCharacterSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not cls.powershell:
            raise unittest.SkipTest("PowerShell is unavailable")

    def _resolve(self, function: str, project_root: Path, value: str) -> list[str]:
        escaped_helper = str(HELPER).replace("'", "''")
        escaped_root = str(project_root).replace("'", "''")
        escaped_value = value.replace("'", "''")
        script = (
            f". '{escaped_helper}'; "
            f"$result=@({function} -ProjectRoot '{escaped_root}' -CharacterName '{escaped_value}'); "
            "ConvertTo-Json -Compress -InputObject $result"
        )
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout.strip())
        return result if isinstance(result, list) else [result]

    def test_environment_module_uses_the_executed_candidate_resolvers(self):
        source = ENV_MODULE.read_text(encoding="utf-8-sig")
        self.assertIn('. (Join-Path $PSScriptRoot "character_source.ps1")', source)
        self.assertIn("Resolve-InstallerStaticCharacterCandidates", source)
        self.assertIn("Resolve-InstallerSequenceCharacterDirectories", source)

    def test_legacy_relative_static_maps_to_canonical_before_deleted_flat_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = self._resolve(
                "Resolve-InstallerStaticCharacterCandidates", root, "resource/character/arona.png"
            )
        self.assertEqual(Path(values[0]), root / "resource" / "character" / "static" / "arona.png")
        self.assertIn(str(root / "resource" / "character" / "arona.png"), values)

    def test_legacy_relative_sequence_maps_to_canonical_before_deleted_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = self._resolve(
                "Resolve-InstallerSequenceCharacterDirectories", root, "resource/character/smoke_chroma"
            )
        self.assertEqual(Path(values[0]), root / "resource" / "character" / "sequences" / "smoke_chroma")
        self.assertIn(str(root / "resource" / "character" / "smoke_chroma"), values)

    def test_current_checkout_old_absolute_path_maps_but_arbitrary_absolute_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "resource" / "character" / "arona.png"
            mapped = self._resolve("Resolve-InstallerStaticCharacterCandidates", root, str(old))
            arbitrary_path = root / "elsewhere" / "arona.png"
            arbitrary = self._resolve("Resolve-InstallerStaticCharacterCandidates", root, str(arbitrary_path))
        self.assertIn(str(root / "resource" / "character" / "static" / "arona.png"), mapped)
        self.assertEqual(arbitrary, [str(arbitrary_path)])


if __name__ == "__main__":
    unittest.main()
