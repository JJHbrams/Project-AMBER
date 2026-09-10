"""Isolated Windows component planning/ownership tests; never install into a profile."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'installer/external-components.ps1'
IDS = ['bolttagu-2d', 'rabbit-2d', 'robot-arm', 'xeyes', 'robot-arm-3d', 'robot-arm-3d-v2', 'robot-arm-3d-v3']


@unittest.skipUnless(os.name == 'nt', 'PowerShell Windows fixture')
class ExternalComponentsTests(unittest.TestCase):
    def run_ps(self, body):
        for shell in ('powershell', 'pwsh'):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command',
                    f"$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest; . '{HELPER}';\n" + body],
                    capture_output=True, encoding='utf-8', errors='replace', timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def manifest(self, directory):
        data = {'schema': 1, 'package_version': '1.1.2.999', 'registry_revision': 'fixture',
                'common': {'file': 'core.whl', 'sha256': '0' * 64},
                'components': [{'id': key, 'display_name': key, 'group': '3d' if '3d' in key else '2d',
                                'payloads': [key], 'requires': []} for key in IDS],
                'payloads': [{'id': key, 'file': key + '.zip', 'sha256': '0' * 64,
                              'requires': ['resource.v2'] if key == IDS[-1] else [], 'files': []} for key in IDS]}
        data['payloads'].append({'id': 'resource.v2', 'file': 'shared.zip', 'sha256': '0' * 64, 'requires': [], 'files': []})
        path = Path(directory) / 'manifest.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_changed_powershell_sources_parse_in_both_windows_shells(self):
        paths = ['INSTALL.ps1', 'dev-rebuild.ps1', 'installer/install.ps1', 'installer/configure.ps1',
                 'installer/external-components.ps1', 'installer/component-status.ps1',
                 'installer/build-components.ps1', 'installer/external-bundle.ps1', 'installer/joint-startup.ps1',
                 'installer/modules/02_interactive.ps1', 'installer/modules/10_shortcuts.ps1']
        quoted = ','.join("'" + str(ROOT / path) + "'" for path in paths)
        self.run_ps(f"""
foreach ($path in @({quoted})) {{
  $parseErrors=$null
  [Management.Automation.Language.Parser]::ParseFile($path,[ref]$null,[ref]$parseErrors) | Out-Null
  if ($parseErrors) {{ throw ($path + ': ' + ($parseErrors | Out-String)) }}
}}
""")

    def test_hidden_dependency_does_not_select_v2_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.manifest(tmp)
            self.run_ps(f"""
$plan = Get-EngramComponentPlan '{manifest}' -Components 'robot-arm-3d-v3' -Installed @('rabbit-2d')
if (($plan.InstalledComponents -join ',') -ne 'rabbit-2d,robot-arm-3d-v3') {{ throw 'Visible selection changed' }}
if ('resource.v2' -notin $plan.InstalledPayloads) {{ throw 'Missing hidden dependency' }}
if ('robot-arm-3d-v2' -in $plan.InstalledComponents) {{ throw 'Hidden resource advertised as preset' }}
$rejected=$false; try {{ Get-EngramComponentPlan '{manifest}' -Components '../bad' }} catch {{ $rejected=$true }}
if (-not $rejected) {{ throw 'Unknown selection accepted' }}
""")

    def test_pointer_ownership_hash_and_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = 'generations/' + 'a' * 32
            generation = root / relative
            generation.mkdir(parents=True)
            marker = generation / 'engram-overlay-components.json'
            marker.write_text(json.dumps({'schema': 1, 'ownership': {'owner': 'engram', 'layout': 'generation-venv'},
                                         'manifest_sha256': 'a' * 64, 'package_version': '1.1.2.158',
                                         'registry_revision': 'external-overlay-components-v1',
                                         'installed_components': ['rabbit-2d'], 'installed_payloads': ['overlay.rabbit-2d']}), encoding='utf-8')
            pointer = root / 'active-generation.json'
            pointer.write_text(json.dumps({'schema': 1, 'owner': 'engram', 'generation': relative,
                                           'marker_sha256': hashlib.sha256(marker.read_bytes()).hexdigest()}), encoding='utf-8')
            self.run_ps(f"""
$active = Get-EngramComponentGeneration '{root}'
if ($active.Runtime -notlike '*venv') {{ throw 'Runtime resolution failed' }}
$rejected=$false; try {{ Assert-EngramComponentPath '{root}' (Join-Path '{root}' '../outside') }} catch {{ $rejected=$true }}
if (-not $rejected) {{ throw 'Traversal accepted' }}
""")
            marker.write_text('{}', encoding='utf-8')
            self.run_ps(f"""
$rejected=$false; try {{ Get-EngramComponentGeneration '{root}' }} catch {{ $rejected=$true }}
if (-not $rejected) {{ throw 'Tampered marker accepted' }}
""")

    def test_legacy_runtime_is_not_adopted_or_mutated(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / 'runtime'
            runtime.mkdir()
            sentinel = runtime / 'user.txt'
            sentinel.write_text('keep', encoding='utf-8')
            self.run_ps(f"""
$rejected=$false; try {{ Install-EngramExternalComponents -Root '{tmp}' -ManifestPath 'absent' -Components 'rabbit-2d' }} catch {{ $rejected=$true }}
if (-not $rejected) {{ throw 'Unowned runtime accepted' }}
""")
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertFalse((Path(tmp) / 'active-generation.json').exists())

    def test_owned_startup_preserve_retargets_and_rolls_back_only_exact_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = 'HKCU:\\Software\\EngramComponentFixture-' + uuid.uuid4().hex
            self.run_ps(fr"""
$key='{key}'; $state='{tmp}'
New-Item -Path $key -Force | Out-Null
$old='C:\fixture-old\venv'; $next='C:\fixture-next\venv'
$previous='"C:\fixture-old\venv\Scripts\engram-custom-overlayw.exe" --provider'
$record=@{{owner='engram-joint-startup-v1';host='host.exe';arguments='';external=$previous}}
$recordPath=Join-Path $state 'joint-startup.json'
[IO.File]::WriteAllText($recordPath, ($record | ConvertTo-Json))
try {{
  $none=Update-EngramComponentStartup -PreviousRuntime $old -NextRuntime $next -StateDirectory $state -RunKey $key
  if ($none.Changed) {{ throw 'Disabled login was enabled' }}
  New-ItemProperty -Path $key -Name EngramOverlay -Value $previous -PropertyType String | Out-Null
  $change=Update-EngramComponentStartup -PreviousRuntime $old -NextRuntime $next -StateDirectory $state -RunKey $key
  if (-not $change.Changed) {{ throw 'Owned enabled entry not retargeted' }}
  Undo-EngramComponentStartup $change
  if ((Get-ItemProperty $key).EngramOverlay -cne $previous) {{ throw 'Rollback failed' }}
  Set-ItemProperty $key -Name EngramOverlay -Value 'user-command'
  $custom=Update-EngramComponentStartup -PreviousRuntime $old -NextRuntime $next -StateDirectory $state -RunKey $key
  if ($custom.Changed -or (Get-ItemProperty $key).EngramOverlay -cne 'user-command') {{ throw 'User entry changed' }}
}} finally {{ Remove-Item -LiteralPath $key }}
""")

    def test_interrupted_startup_update_recovers_when_pointer_was_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = 'HKCU:\\Software\\EngramComponentFixture-' + uuid.uuid4().hex
            self.run_ps(fr"""
$key='{key}'; $root='{tmp}'; $state=Join-Path $root 'state'
New-Item -Path $key -Force | Out-Null
New-Item -ItemType Directory -Path $state -Force | Out-Null
$old=Join-Path $root 'runtime'
$next=Join-Path $root 'generations/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/venv'
$previous='"{{0}}" --provider' -f (Join-Path $old 'Scripts\engram-custom-overlayw.exe')
$record=@{{owner='engram-joint-startup-v1';host='host.exe';arguments='';external=$previous}}
[IO.File]::WriteAllText((Join-Path $state 'joint-startup.json'), ($record | ConvertTo-Json))
try {{
  New-ItemProperty -Path $key -Name EngramOverlay -Value $previous -PropertyType String | Out-Null
  $pending=Update-EngramComponentStartup -PreviousRuntime $old -NextRuntime $next -StateDirectory $state -RunKey $key
  if (-not $pending.Changed) {{ throw 'Fixture did not reach pending startup state' }}
  Repair-EngramComponentStartup -Root $root -StateDirectory $state -RunKey $key
  if ((Get-ItemProperty $key).EngramOverlay -cne $previous) {{ throw 'Interrupted startup did not recover' }}
  Repair-EngramComponentStartup -Root $root -StateDirectory $state -RunKey $key
  if ((Get-ItemProperty $key).EngramOverlay -cne $previous) {{ throw 'Recovery is not idempotent' }}
}} finally {{ Remove-Item -LiteralPath $key }}
""")

    def test_nostart_chain_reconciles_on_later_normal_preserve_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generations = []
            for letter in 'abcd':
                generation = root / 'generations' / (letter * 32)
                (generation / 'venv').mkdir(parents=True)
                marker = generation / 'engram-overlay-components.json'
                marker.write_text(json.dumps({'schema': 1, 'package_version': '1.1.2.158',
                    'registry_revision': 'external-overlay-components-v1', 'manifest_sha256': 'a' * 64,
                    'ownership': {'owner': 'engram', 'layout': 'generation-venv'},
                    'installed_components': ['rabbit-2d'], 'installed_payloads': ['overlay.rabbit-2d']}))
                generations.append(generation)
            (root / 'active-generation.json').write_text(json.dumps({'schema': 1, 'owner': 'engram',
                'generation': 'generations/' + 'c' * 32,
                'marker_sha256': hashlib.sha256((generations[2] / 'engram-overlay-components.json').read_bytes()).hexdigest()}))
            key = 'HKCU:\\Software\\EngramComponentFixture-' + uuid.uuid4().hex
            self.run_ps(fr"""
. '{ROOT / 'installer/joint-startup.ps1'}'
$root='{root}'; $state=Join-Path $root 'state'; $key='{key}'
New-Item -ItemType Directory -Path $state -Force | Out-Null
New-Item -Path $key -Force | Out-Null
$old='{generations[0] / 'venv'}'; $middle='{generations[1] / 'venv'}'; $next='{generations[2] / 'venv'}'; $last='{generations[3] / 'venv'}'
$oldValue='"{{0}}" --provider' -f (Join-Path $old 'Scripts\engram-custom-overlayw.exe')
$newValue='"{{0}}" --provider' -f (Join-Path $next 'Scripts\engram-custom-overlayw.exe')
$record=@{{owner='engram-joint-startup-v1';host='host.exe';arguments='';external=$oldValue}}
[IO.File]::WriteAllText((Join-Path $state 'joint-startup.json'), ($record | ConvertTo-Json))
function Set-FixtureGeneration($letter) {{
  $relative='generations/' + ($letter * 32)
  $marker=Join-Path (Join-Path $root $relative) 'engram-overlay-components.json'
  Write-EngramComponentJsonAtomic -Path (Join-Path $root 'active-generation.json') -Value @{{schema=1;owner='engram';generation=$relative;marker_sha256=(Get-EngramComponentHash $marker)}}
}}
Set-FixtureGeneration 'c'
try {{
  New-ItemProperty -Path $key -Name EngramOverlay -Value $oldValue -PropertyType String | Out-Null
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $old -NextRuntime $middle
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $middle -NextRuntime $next
  if ((Get-ItemProperty $key).EngramOverlay -cne $oldValue) {{ throw 'NoStart wrote login registration' }}
  Set-EngramJointStartup -Mode preserve -ExternalRoot $root -StateDirectory $state -RunKey $key
  if ((Get-ItemProperty $key).EngramOverlay -cne $newValue) {{ throw 'Normal preserve did not reconcile deferred chain' }}
  Set-EngramJointStartup -Mode preserve -ExternalRoot $root -StateDirectory $state -RunKey $key
  if ((Get-ItemProperty $key).EngramOverlay -cne $newValue) {{ throw 'Preserve reconciliation is not idempotent' }}
  # The next candidate is journaled, but its pointer publication is interrupted.
  Set-ItemProperty $key -Name EngramOverlay -Value $oldValue
  [IO.File]::WriteAllText((Join-Path $state 'joint-startup.json'), ($record | ConvertTo-Json))
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $old -NextRuntime $middle
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $middle -NextRuntime $next
  Set-FixtureGeneration 'b'
  Set-EngramJointStartup -Mode preserve -ExternalRoot $root -StateDirectory $state -RunKey $key
  $middleValue='"{{0}}" --provider' -f (Join-Path $middle 'Scripts\engram-custom-overlayw.exe')
  if ((Get-ItemProperty $key).EngramOverlay -cne $middleValue) {{ throw 'Unpublished candidate replaced last successful NoStart generation' }}
  # A->B published; B->C only journaled; another NoStart B->D publishes.
  # Login still points at A throughout all three attempts.
  Set-ItemProperty $key -Name EngramOverlay -Value $oldValue
  [IO.File]::WriteAllText((Join-Path $state 'joint-startup.json'), ($record | ConvertTo-Json))
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $old -NextRuntime $middle
  Set-FixtureGeneration 'b'
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $middle -NextRuntime $next
  Save-EngramDeferredComponentStartup -Root $root -PreviousRuntime $middle -NextRuntime $last
  Set-FixtureGeneration 'd'
  if ((Get-ItemProperty $key).EngramOverlay -cne $oldValue) {{ throw 'NoStart retry modified login registration' }}
  Set-EngramJointStartup -Mode preserve -ExternalRoot $root -StateDirectory $state -RunKey $key
  $lastValue='"{{0}}" --provider' -f (Join-Path $last 'Scripts\engram-custom-overlayw.exe')
  if ((Get-ItemProperty $key).EngramOverlay -cne $lastValue) {{ throw 'Retry after unpublished candidate lost original login target' }}
}} finally {{ Remove-Item -LiteralPath $key }}
""")

    @unittest.skipUnless(os.environ.get('ENGRAM_COMPONENT_BUNDLE'), 'explicit isolated real pip fixture opt-in')
    def test_actual_selected_install_and_sdk(self):
        bundle = Path(os.environ['ENGRAM_COMPONENT_BUNDLE']).resolve()
        with tempfile.TemporaryDirectory(prefix='engram-components-real-') as tmp:
            damaged_bundle = Path(tmp) / 'damaged-bundle'
            shutil.copytree(bundle, damaged_bundle)
            (damaged_bundle / 'payloads/overlay.robot-arm-3d-v3.zip').write_bytes(b'corrupt')
            body = f"""
$ErrorActionPreference='Stop'
. '{ROOT / 'installer/external-overlay.ps1'}'
$result=Install-EngramExternalComponents -Root '{tmp}' -ManifestPath '{bundle / 'engram-overlay-components.json'}' -Components 'robot-arm-3d-v3' -PythonCommand '{os.sys.executable}'
if (-not $result.Installed) {{ throw 'Actual component install failed' }}
$active=Get-EngramComponentGeneration '{tmp}'
if (($active.Marker.installed_components -join ',') -ne 'robot-arm-3d-v3') {{ throw 'Unselected preset installed' }}
$oldGeneration=$active.Generation
$again=Install-EngramExternalComponents -Root '{tmp}' -ManifestPath '{bundle / 'engram-overlay-components.json'}' -Components 'rabbit-2d' -PythonCommand '{os.sys.executable}'
$active=Get-EngramComponentGeneration '{tmp}'
if (($active.Marker.installed_components -join ',') -ne 'rabbit-2d,robot-arm-3d-v3') {{ throw 'Upgrade lost existing selected component' }}
if ($active.Generation -eq $oldGeneration -or -not (Test-Path $oldGeneration)) {{ throw 'Upgrade relocated/deleted previous generation' }}
$pointerBefore=[IO.File]::ReadAllText((Join-Path '{tmp}' 'active-generation.json'))
$failed=$false
try {{ Install-EngramExternalComponents -Root '{tmp}' -ManifestPath '{damaged_bundle / 'engram-overlay-components.json'}' -Components 'xeyes' -PythonCommand '{os.sys.executable}' | Out-Null }} catch {{ $failed=$true }}
if (-not $failed -or [IO.File]::ReadAllText((Join-Path '{tmp}' 'active-generation.json')) -cne $pointerBefore) {{ throw 'Failed upgrade changed active generation' }}
$sdk=Install-EngramOverlaySdk -Root '{tmp}' -ManifestPath '{bundle / 'engram-overlay-components.json'}'
if (-not (Test-Path (Join-Path $sdk 'examples/minimal-v2-client.py'))) {{ throw 'SDK example missing' }}
"""
            env = os.environ.copy()
            env.pop('PSModulePath', None)
            result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', body],
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=300, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            active = json.loads((Path(tmp) / 'active-generation.json').read_text())
            marker = json.loads((Path(tmp) / active['generation'] / 'engram-overlay-components.json').read_text())
            self.assertEqual(marker['installed_components'], ['rabbit-2d', 'robot-arm-3d-v3'])
            site = Path(tmp) / active['generation'] / 'venv/Lib/site-packages/engram_overlay'
            self.assertFalse((site / 'assets/bolttagu').exists())
            self.assertFalse((site / 'overlays/bolttagu_2d.py').exists())


if __name__ == '__main__':
    unittest.main()
