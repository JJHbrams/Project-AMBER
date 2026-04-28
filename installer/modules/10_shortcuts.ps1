#
# 10_shortcuts.ps1 — Start Menu 바로가기, Startup 자동시작, KG Watcher VBS 등록
#

$DistExe = Join-Path $ProjectRoot "dist\engram-overlay\engram-overlay.exe"
$OverlayCmdPath = Join-Path $ShimDir "engram-overlay.cmd"

# 12. Start Menu shortcut (Windows Search)
Write-Step "Start Menu shortcut..."
$StartMenuDir = [Environment]::GetFolderPath("Programs")
$StartMenuLink = Join-Path $StartMenuDir "Engram Overlay.lnk"
if (Test-Path $OverlayCmdPath) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($StartMenuLink)
    $shortcut.TargetPath = $OverlayCmdPath
    $shortcut.WorkingDirectory = $ShimDir
    $shortcut.Description = "Engram Overlay"
    $shortcut.Save()
    Write-Ok $StartMenuLink
} elseif (Test-Path $DistExe) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($StartMenuLink)
    $shortcut.TargetPath = $DistExe
    $shortcut.WorkingDirectory = Split-Path $DistExe
    $shortcut.Description = "Engram Overlay"
    $shortcut.Save()
    Write-Ok $StartMenuLink
} else { Write-Warn "Skipped — launcher/exe not found" }

# 13. Startup shortcut (auto-start on boot)
$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupLink = Join-Path $StartupDir "engram-overlay.lnk"
if ($EnableAutoStart) {
    Write-Step "Startup registration (자동시작)..."
    if (Test-Path $OverlayCmdPath) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($StartupLink)
        $shortcut.TargetPath = $OverlayCmdPath
        $shortcut.WorkingDirectory = $ShimDir
        $shortcut.Description = "Engram Overlay — Auto Start"
        $shortcut.Save()
        Write-Ok $StartupLink
    } elseif (Test-Path $DistExe) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($StartupLink)
        $shortcut.TargetPath = $DistExe
        $shortcut.WorkingDirectory = Split-Path $DistExe
        $shortcut.Description = "Engram Overlay — Auto Start"
        $shortcut.Save()
        Write-Ok $StartupLink
    } else { Write-Warn "Skipped — launcher/exe not found" }
} else {
    Write-Step "Startup registration (건너뜀 — 사용자 선택)..."
    if (Test-Path $StartupLink) {
        Remove-Item $StartupLink -Force
        Write-Ok "기존 자동시작 등록 제거: $StartupLink"
    } else {
        Write-Ok "자동시작 미등록 (수동 실행)"
    }
}

# 14. KG Watcher — overlay.exe의 자식 프로세스로 관리되므로 별도 등록 없음
# kg_watcher는 overlay 기동 시 _deferred_startup()에서 자동 시작/종료된다.
Write-Step "KG Watcher — managed by overlay (no separate registration)"
# 기존에 등록된 VBS가 있으면 정리
$WatcherVbs = Join-Path $StartupDir "engram-kg-watcher.vbs"
if (Test-Path $WatcherVbs) {
    Remove-Item $WatcherVbs -Force
    Write-Ok "Removed legacy VBS: $WatcherVbs"
    # 고아 프로세스도 정리
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "kg_watcher\.py" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
} else {
    Write-Ok "No legacy VBS found (already clean)"
}
