import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SmokeProfileTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows installer cache')
    def test_real_installer_signature_changes_with_external_helper(self):
        script = r'''
$ErrorActionPreference = 'Stop'
. ./installer/build-cache.ps1
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('engram-cache-test-' + [Guid]::NewGuid().ToString('N'))
$installer = Join-Path $fixture 'installer'
$dist = Join-Path $fixture 'dist'
New-Item -ItemType Directory -Path $installer,$dist | Out-Null
$helper = Join-Path $installer 'joint-startup.ps1'
[IO.File]::WriteAllText($helper, 'first')
$first = Get-EngramInstallerSignature $fixture $dist 'test'
[IO.File]::WriteAllText($helper, 'second')
$second = Get-EngramInstallerSignature $fixture $dist 'test'
if ($first -eq $second -or $second.Length -ne 64) { throw 'Installer helper change did not invalidate cache' }
Write-Output 'CACHE_SIGNATURE_PASS'
'''
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CACHE_SIGNATURE_PASS', result.stdout)

    def test_installer_cache_covers_external_runtime_payload_and_helpers(self):
        cache = (ROOT / 'installer/build-cache.ps1').read_text(encoding='utf-8-sig')
        for filename in ('joint-startup.ps1', 'external-wheel.ps1', 'external-overlay.ps1',
                         'external-overlay.pin', 'external-overlay.whl'):
            self.assertIn('installer\\' + filename, cache)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles')
    def test_real_artifact_stop_keeps_other_directory_process_alive(self):
        script = r'''
$ErrorActionPreference = 'Stop'
. ./installer/stop-engram-processes.ps1
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('engram-stop-test-' + [Guid]::NewGuid().ToString('N'))
$target = Join-Path $fixture 'target'
$foreign = Join-Path $fixture 'foreign'
New-Item -ItemType Directory -Path $target,$foreign | Out-Null
$targetExe = Join-Path $target 'engram-overlay.exe'
$foreignExe = Join-Path $foreign 'engram-overlay.exe'
Add-Type -TypeDefinition 'public class SafeFixture { public static void Main() { System.Threading.Thread.Sleep(60000); } }' -OutputAssembly $targetExe -OutputType ConsoleApplication
Copy-Item -LiteralPath $targetExe -Destination $foreignExe
$first = $null
$second = $null
try {
    $first = Start-Process -FilePath $targetExe -PassThru -WindowStyle Hidden
    $second = Start-Process -FilePath $foreignExe -PassThru -WindowStyle Hidden
    $firstHandle = $first.Handle
    $secondHandle = $second.Handle
    $result = Stop-EngramArtifactProcesses -ArtifactDir $target
    if (-not $first.WaitForExit(5000) -or $second.HasExited) { throw 'Wrong process termination' }
    if (@($result.StoppedPaths).Count -ne 1 -or $result.StoppedPaths[0] -ne $targetExe) { throw 'Incorrect stop evidence' }
    Write-Output 'HANDLE_STOP_PASS'
} finally {
    foreach ($owned in @($first,$second)) {
        if ($owned -and -not $owned.HasExited) { $owned.Kill(); $owned.WaitForExit() }
    }
}
'''
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('HANDLE_STOP_PASS', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell profile')
    def test_real_profile_scope_is_separate_and_restored(self):
        script = r'''
$ErrorActionPreference = 'Stop'
. ./installer/smoke-profile.ps1
$originalProfile = $env:USERPROFILE
$originalAppData = $env:APPDATA
$saved = Enter-EngramBuildSmokeProfile
try {
    if ($env:USERPROFILE -eq $originalProfile) { throw 'Profile was not isolated' }
    foreach ($path in @($env:USERPROFILE,$env:APPDATA,$env:LOCALAPPDATA,$env:ENGRAM_SMOKE_DB_DIR,$env:CODEX_HOME,$env:CLAUDE_CONFIG_DIR)) {
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw 'Missing profile directory' }
        if (-not $path.StartsWith($env:USERPROFILE + '\') -and $path -ne $env:USERPROFILE) { throw 'Escaped smoke profile' }
    }
    $first = Get-Content (Join-Path $env:USERPROFILE '.engram/user.config.yaml') -Raw | ConvertFrom-Json
    $second = Get-Content (Join-Path $env:USERPROFILE '.engram/overlay.user.yaml') -Raw | ConvertFrom-Json
    if ($first.mcp.http_port -ne $second.mcp.http_port -or $first.overlay.stm_server_port -ne $second.overlay.stm_server_port) { throw 'Port disagreement' }
    if ($env:ENGRAM_BUILD_SMOKE -ne '1') { throw 'Offline smoke guard missing' }
} finally { Exit-EngramBuildSmokeProfile $saved }
if ($env:USERPROFILE -ne $originalProfile -or $env:APPDATA -ne $originalAppData) { throw 'Environment not restored' }
Write-Output 'PROFILE_PASS'
'''
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PROFILE_PASS', result.stdout)

    def test_dashboard_smoke_cannot_contact_interactive_mcp(self):
        from core.dashboard import semantic_api
        with patch.dict(os.environ, {'ENGRAM_BUILD_SMOKE': '1'}), \
             patch.object(semantic_api.urllib.request, 'urlopen') as request:
            self.assertIsNone(semantic_api._sg_api('/api/sg/stats'))
            request.assert_not_called()

    def test_dashboard_uses_effective_mcp_port(self):
        from core.dashboard import semantic_api
        with patch.dict(os.environ, {'ENGRAM_BUILD_SMOKE': '0'}), \
             patch('core.install.service_config.effective_service_config', return_value={'mcp': {'http_port': 49231}}), \
             patch.object(semantic_api.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"enabled":true}'
            self.assertEqual(semantic_api._sg_api('/api/sg/stats'), {'enabled': True})
            self.assertEqual(request.call_args.args[0].full_url, 'http://127.0.0.1:49231/api/sg/stats')

    def test_dashboard_bad_configuration_does_not_fallback_to_default_port(self):
        from core.dashboard import semantic_api
        with patch.dict(os.environ, {'ENGRAM_BUILD_SMOKE': '0'}), \
             patch('core.install.service_config.effective_service_config', side_effect=ValueError('bad port')), \
             patch.object(semantic_api.urllib.request, 'urlopen') as request:
            self.assertIsNone(semantic_api._sg_api('/api/sg/stats'))
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
