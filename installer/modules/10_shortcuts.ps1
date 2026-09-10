#
# 10_shortcuts.ps1 — Start Menu 바로가기, Startup 자동시작, KG Watcher VBS 등록
#

$DistExe = Join-Path $ProjectRoot "dist\engram-overlay\engram-overlay.exe"
$OverlayCmdPath = Join-Path $ShimDir "engram-overlay.cmd"

# 12. Start Menu shortcut (Windows Search)
# 바로가기는 exe 를 직접 가리켜야 한다:
#   - Windows 는 타깃이 .exe 가 아니면(.cmd 등) '작업표시줄에 고정' 을 비활성화한다
#   - .cmd 경유 실행은 부팅/실행 시 콘솔창이 깜빡이고 AppUserModelID 연결이 끊긴다
# (.cmd 는 터미널에서 `engram-overlay` 로 호출하는 PATH 용으로만 유지)
Write-Step "Start Menu shortcut..."
$StartMenuDir = [Environment]::GetFolderPath("Programs")
$StartMenuLink = Join-Path $StartMenuDir "AMBER (ENGRAM).lnk"
$LegacyStartMenuLink = Join-Path $StartMenuDir "Engram Overlay.lnk"
if (Test-Path $DistExe) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($StartMenuLink)
    $shortcut.TargetPath = $DistExe
    $shortcut.WorkingDirectory = Split-Path $DistExe
    $shortcut.Description = "AMBER (ENGRAM)"
    $shortcut.IconLocation = "$DistExe,0"
    $shortcut.Save()
    if (Test-Path $LegacyStartMenuLink) { Remove-Item $LegacyStartMenuLink -Force }
    Write-Ok $StartMenuLink
} elseif (Test-Path $OverlayCmdPath) {
    # exe 가 아직 없을 때만 .cmd 폴백 (이 경우 작업표시줄 고정 불가)
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($StartMenuLink)
    $shortcut.TargetPath = $OverlayCmdPath
    $shortcut.WorkingDirectory = $ShimDir
    $shortcut.Description = "AMBER (ENGRAM)"
    $shortcut.Save()
    if (Test-Path $LegacyStartMenuLink) { Remove-Item $LegacyStartMenuLink -Force }
    Write-Warn "$StartMenuLink (exe 미빌드 — .cmd 폴백, 작업표시줄 고정 불가)"
} else { Write-Warn "Skipped — launcher/exe not found" }

# 13. Startup shortcut (auto-start on boot)
Initialize-EngramExternalRuntime -Mode $ExternalOverlayMode -HostExecutable $DistExe -WheelPath (Join-Path $PSScriptRoot '..\external-overlay.whl') -PythonCommand $PythonExe -AllowDownloadWheel
if ($ExternalOverlayComponents) {
    . (Join-Path $PSScriptRoot '..\external-overlay.ps1')
    . (Join-Path $PSScriptRoot '..\external-bundle.ps1')
    $existingComponents = (Get-EngramInstalledComponents).InstalledComponents
    $requestedComponents = @(@($ExternalOverlayComponents.Split(',')) + @($existingComponents) | Where-Object { $_ } | Sort-Object -Unique) -join ','
    $componentManifest = Resolve-EngramComponentBundle -Components $requestedComponents -Sdk:($ExternalOverlaySdk -eq 'yes')
    Install-EngramExternalComponents -ManifestPath $componentManifest -Components $ExternalOverlayComponents -PythonCommand $PythonExe -UpdateStartup:(-not $NoStart) | Out-Host
}
if ($ExternalOverlaySdk -eq 'yes') {
    . (Join-Path $PSScriptRoot '..\external-bundle.ps1')
    $sdkManifest = Resolve-EngramComponentBundle -Sdk
    $sdkPath = Install-EngramOverlaySdk -ManifestPath $sdkManifest
    Write-Ok "External overlay SDK: $sdkPath"
}
$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupLink = Join-Path $StartupDir "AMBER (ENGRAM).lnk"
$LegacyStartupLink = Join-Path $StartupDir "engram-overlay.lnk"

# Preserve existing login settings on unattended reinstalls; only the explicit
# CLI option or interactive choice changes joint startup. NoStart is read-only
# with respect to login registration as well as launching processes.
if (-not $NoStart) {
    Set-EngramJointStartup -Mode $AutoStart -HostExecutable $DistExe -HostOnly:($JointStartupHostOnly -or $ExternalOverlayMode -eq 'none')
}

# 14. KG Watcher — overlay.exe의 자식 프로세스로 관리되므로 별도 등록 없음
# kg_watcher는 overlay 기동 시 _deferred_startup()에서 자동 시작/종료된다.
Write-Step "KG Watcher — managed by overlay (no separate registration)"
# 기존에 등록된 VBS가 있으면 정리
$WatcherVbs = Join-Path $StartupDir "engram-kg-watcher.vbs"
if (Test-Path $WatcherVbs) {
    Write-Warn "Legacy watcher startup found; preserved pending ownership review: $WatcherVbs"
} else {
    Write-Ok "No legacy VBS found (already clean)"
}
