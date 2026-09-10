"""Real Windows COM/registry tests in an isolated folder and unique HKCU key.

Fixture binaries are never executed: these test registration, not renderer health.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows startup integration')
class JointStartupTests(unittest.TestCase):
    def test_dev_native_autostart_preflight_does_not_require_catalog(self):
        # Evaluate actual preflight statements only; no hook/runtime/startup writes.
        source = (ROOT / 'dev-rebuild.ps1').read_text(encoding='utf-8-sig')
        preflight = source.split('Write-Step "Restarting canonical source entrypoint"', 1)[1].split('$previousDevRestartMarker', 1)[0]
        for shell in ('powershell', 'pwsh'):
            with self.subTest(shell=shell):
                script = """
$ErrorActionPreference='Stop'
function Get-EngramExistingStartupHost { $script:hosts++; 'fixture-host.exe' }
function Get-EngramCatalogRuntime { $script:catalogs++; if ($script:missing) { throw 'Fixture catalog missing' } }
$AutoStart='on'; $ExternalOverlay='start'; $PSBoundParameters=@{}
$contract=[pscustomobject]@{ selected_renderer_id='' }
$script:hosts=0; $script:catalogs=0; $script:missing=$true
""" + preflight + """
if ($script:hosts -ne 1 -or $script:catalogs -ne 0) { throw 'Native autostart incorrectly depends on catalog' }
$contract.selected_renderer_id='other.renderer'
$guarded=$false
try {
""" + preflight + """
} catch { $guarded=$true }
if (-not $guarded -or $script:catalogs -eq 0) { throw 'Selected external renderer lost missing-runtime guard' }
$contract.selected_renderer_id=''; $script:catalogs=0
$PSBoundParameters=@{ExternalOverlay='start'}
$guarded=$false
try {
""" + preflight + """
} catch { $guarded=$true }
if (-not $guarded -or $script:catalogs -ne 1) { throw 'Explicit external start lost missing-runtime guard' }
Write-Output 'PASS'
"""
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('PASS', result.stdout)

    def test_install_host_only_launch_does_not_require_external(self):
        # Exercise the actual final launch statement with an intercepted launch,
        # not all installation phases or any user's runtime/profile.
        for shell in ('powershell', 'pwsh'):
            with self.subTest(shell=shell):
                script = f"""
$ErrorActionPreference='Stop'
$line = Get-Content -LiteralPath '{ROOT / 'installer/install.ps1'}' | Where-Object {{ $_ -match '^ +Invoke-EngramInstalledLaunch ' }}
function Invoke-EngramInstalledLaunch {{ param($Executable,$ExternalOverlay,[switch]$RequireExternal); $script:required = [bool]$RequireExternal }}
$DistExe='C:/fixture-not-executed/engram-overlay.exe'; $ExternalOverlay='start'; $AutoStart='on'
$ExternalOverlayMode='none'; $JointStartupHostOnly=$true
& ([scriptblock]::Create($line))
if ($script:required) {{ throw 'Host-only choice incorrectly requires external runtime' }}
$JointStartupHostOnly=$false
& ([scriptblock]::Create($line))
if ($script:required) {{ throw 'Native default must not require an external runtime for autostart' }}
$JointStartupHostOnly=$true; $ExternalOverlayMode='bolttagu-2d'
& ([scriptblock]::Create($line))
if (-not $script:required) {{ throw 'Explicit external choice lost requirement' }}
Write-Output 'PASS'
"""
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('PASS', result.stdout)

    def test_real_python_eligibility_on_windows_powershell(self):
        import sys
        for shell in ('powershell', 'pwsh'):
            with self.subTest(shell=shell):
                script = f". '{ROOT / 'installer/external-overlay.ps1'}'; $result = Resolve-EligiblePython -Command '{sys.executable}'; if (-not $result.Ok) {{ throw $result.Reason }}; Write-Output 'PASS'"
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('PASS', result.stdout)

    def test_registration_matrix_powershell_5_and_7(self):
        shells = [shutil.which(name) for name in ('powershell', 'pwsh')]
        self.assertTrue(all(shells), 'Both Windows PowerShell 5 and PowerShell 7 required')
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runtime = root / 'external/runtime'
                (runtime / 'Scripts').mkdir(parents=True)
                pkg = runtime / 'Lib/site-packages/engram_overlay'
                pkg.mkdir(parents=True)
                (runtime / 'Scripts/engram-custom-overlayw.exe').touch()
                (pkg / '__main__.py').write_text('run_catalog_provider', encoding='utf-8')
                (pkg / 'provider.py').touch()
                host = root / 'engram-overlay.exe'
                host.touch()
                (runtime / 'pyvenv.cfg').write_text(f'executable = {host}\n', encoding='utf-8')
                key = 'HKCU:\\Software\\EngramJointStartupTests\\' + uuid.uuid4().hex
                script = f"""
$ErrorActionPreference = 'Stop'
. '{ROOT / 'installer/joint-startup.ps1'}'
$p = @{{ HostExecutable='{host}'; StartupDirectory='{root / 'startup'}'; StateDirectory='{root / 'state'}'; ExternalRoot='{root / 'external'}'; RunKey='{key}' }}
function Assert($condition, $message) {{ if (-not $condition) {{ throw $message }} }}
try {{
  Set-EngramJointStartup @p -Mode preserve
  Assert (-not (Test-Path -LiteralPath $p.StartupDirectory)) 'preserve wrote startup'
  New-Item -ItemType Directory -Path $p.StartupDirectory | Out-Null
  $legacy = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $p.StartupDirectory 'AMBER (ENGRAM).lnk'))
  $legacy.TargetPath = $p.HostExecutable
  $legacy.Description = 'AMBER (ENGRAM) - Auto Start'
  $legacy.Save()
  $oldPath = Join-Path $p.StartupDirectory 'engram-overlay.lnk'
  $old = (New-Object -ComObject WScript.Shell).CreateShortcut($oldPath)
  $old.TargetPath = $p.HostExecutable
  $old.Description = 'Engram Overlay'
  $old.Save()
  Set-EngramJointStartup @p -Mode on
  Assert (-not (Test-Path -LiteralPath $oldPath)) 'legacy duplicate retained'
  $lnk = Join-Path $p.StartupDirectory 'AMBER (ENGRAM).lnk'
  Assert (Test-Path -LiteralPath $lnk) 'ON missing host'
  Assert ((Get-ItemPropertyValue -LiteralPath $p.RunKey -Name EngramOverlay) -match '--provider$') 'ON missing provider'
  Assert (-not (Test-Path -LiteralPath (Join-Path $p.ExternalRoot 'runtime/.installed-by-amber'))) 'adopted runtime'
  Set-EngramJointStartup @p -Mode on
  Set-EngramJointStartup @p -Mode on -HostOnly
  Set-EngramJointStartup @p -Mode off
  Assert (-not (Test-Path -LiteralPath $lnk)) 'OFF retained owned host'
  Assert (-not (Get-ItemProperty -LiteralPath $p.RunKey -Name EngramOverlay -ErrorAction SilentlyContinue)) 'OFF retained owned Run'
  Set-EngramJointStartup @p -Mode on
  Set-ItemProperty -LiteralPath $p.RunKey -Name EngramOverlay -Value 'user-custom'
  Set-EngramJointStartup @p -Mode off
  Assert ((Get-ItemPropertyValue -LiteralPath $p.RunKey -Name EngramOverlay) -eq 'user-custom') 'OFF changed user Run'
  $failed = $false
  try {{ Set-EngramJointStartup @p -Mode on }} catch {{ $failed = $true }}
  Assert $failed 'ON accepted conflict'
  Assert (-not (Test-Path -LiteralPath $lnk)) 'conflict partially wrote host'
  Remove-ItemProperty -LiteralPath $p.RunKey -Name EngramOverlay
  Remove-Item -LiteralPath (Join-Path $p.ExternalRoot 'runtime/Scripts/engram-custom-overlayw.exe')
  $failed = $false
  try {{ Set-EngramJointStartup @p -Mode on }} catch {{ $failed = $true }}
  Assert $failed 'ON accepted missing runtime'
  Assert (-not (Test-Path -LiteralPath $lnk)) 'missing runtime partially wrote host'
  Set-EngramJointStartup @p -Mode on -HostOnly
  Assert (Test-Path -LiteralPath $lnk) 'host-only install failed'
  Write-Output 'PASS: preserve/on/idempotent/off/unowned/conflict/missing/host-only'
}} finally {{
  if (Test-Path -LiteralPath $p.RunKey) {{ Remove-Item -LiteralPath $p.RunKey -Force }}
}}
"""
                completed = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
                                           capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn('PASS:', completed.stdout)

    def test_entrypoints_reject_nostart_mutation_before_side_effects(self):
        for script in ('INSTALL.ps1', 'installer/install.ps1', 'dev-rebuild.ps1', 'installer/configure.ps1'):
            with self.subTest(script=script):
                arguments = ['-InstallDir', 'C:/fixture-not-executed'] if script.endswith('configure.ps1') else []
                result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-File', str(ROOT / script),
                                         '-NoStart', '-AutoStart', 'on', *arguments], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('NoStart requires', result.stderr)

    def test_generic_wheel_filename_is_staged_from_pinned_metadata(self):
        import zipfile
        version = (ROOT / 'installer/external-overlay.pin').read_text().strip()
        for shell in ('powershell', 'pwsh'):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                wheel = Path(temporary) / 'external-overlay.whl'
                with zipfile.ZipFile(wheel, 'w') as archive:
                    archive.writestr(f'engram_custom_overlay-{version}.dist-info/WHEEL', 'Wheel-Version: 1.0\nTag: py3-none-any\n')
                script = f"""
$ErrorActionPreference='Stop'
. '{ROOT / 'installer/external-overlay.ps1'}'
$stage = New-ExternalOverlayWheelStage -WheelPath '{wheel}'
try {{
 if ((Split-Path $stage.Path -Leaf) -ne 'engram_custom_overlay-{version}-py3-none-any.whl') {{ throw 'Wrong wheel filename' }}
 if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($stage.Path)) -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes('{wheel}'))) {{ throw 'Wheel bytes changed' }}
 Write-Output 'PASS'
}} finally {{ Remove-Item -LiteralPath $stage.Path; Remove-Item -LiteralPath $stage.Directory }}
"""
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('PASS', result.stdout)
