"""Fixed-source acquisition tests; HTTP is intercepted, never a release claim."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'installer/external-components'


@unittest.skipUnless(os.name == 'nt' and BUNDLE.is_dir(), 'Windows staged pinned bundle fixture')
class ExternalBundleTests(unittest.TestCase):
    def test_fresh_cache_fetches_only_pinned_selection_and_sdk(self):
        with tempfile.TemporaryDirectory(prefix='engram-bundle-fetch-') as tmp:
            for shell in ('powershell', 'pwsh'):
                with self.subTest(shell=shell):
                    destination = Path(tmp) / shell
                    body = fr"""
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
. '{ROOT / 'installer/external-bundle.ps1'}'
$script:requests=0
function Invoke-WebRequest {{
  param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec,$ErrorAction)
  if ($Uri -notmatch '^https://github.com/JJHbrams/engram-overlay/releases/download/v[0-9.]+/([A-Za-z0-9_.-]+)$') {{ throw 'Untrusted URL' }}
  $name=$Matches[1]; $source=Join-Path '{BUNDLE}' $name
  if (-not (Test-Path $source)) {{ $source=Join-Path '{BUNDLE / 'payloads'}' $name }}
  [IO.File]::Copy($source,$OutFile); $script:requests++
}}
$manifest=Resolve-EngramComponentBundle -Destination '{destination}' -Components 'rabbit-2d' -Sdk
if ($script:requests -ne 4) {{ throw 'Downloaded unselected artifacts or missed a required artifact' }}
Resolve-EngramComponentBundle -Destination '{destination}' -Components 'rabbit-2d' -Sdk -NoDownload | Out-Null
if ($script:requests -ne 4) {{ throw 'Valid cache unexpectedly redownloaded' }}
$before=$script:requests; $rejected=$false
try {{ Receive-EngramPinnedArtifact -Version 'latest' -FileName '../evil' -Digest ('a'*64) -Destination '{destination / 'bad'}' }} catch {{ $rejected=$true }}
if (-not $rejected -or $script:requests -ne $before) {{ throw 'Unsafe URL request attempted' }}
"""
                    script = Path(tmp) / (shell + '-acquisition.ps1')
                    script.write_text(body, encoding='utf-8-sig')
                    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                                            capture_output=True, encoding='utf-8', errors='replace', timeout=45)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse((destination / 'payloads/overlay.robot-arm-3d-v3.zip').exists())

    def test_generated_inno_names_are_safe_but_public_ids_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix='engram-bundle-include-') as tmp:
            destination = Path(tmp) / 'bundle'
            shutil.copytree(BUNDLE, destination)
            body = f". '{ROOT / 'installer/build-components.ps1'}'; Write-EngramComponentIncludes -BundleRoot '{destination}'"
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', body],
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tree = (destination / 'tree.iss').read_text(encoding='utf-8-sig')
            code = (destination / 'code.iss').read_text(encoding='utf-8-sig')
            self.assertIn('external\\3d\\robot_arm_3d_v3', tree)
            self.assertNotIn('external\\3d\\robot-arm-3d-v3', tree)
            self.assertIn("Result := Result + 'robot-arm-3d-v3'", code)
            self.assertIn("ExistingExternalComponent('robot-arm-3d-v3')", code)
            self.assertIn("WizardIsComponentSelected('external\\3d\\robot_arm_3d_v3')", code)


if __name__ == '__main__':
    unittest.main()
