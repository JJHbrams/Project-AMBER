<#
.SYNOPSIS
    Engram Installer — 오케스트레이터
    Install: .\install.ps1
    Install (overlay build mode): .\install.ps1 -OverlayBuildMode auto|rebuild|clean|skip
    Remove:  .\install.ps1 -Uninstall

    모듈 구조:
      common.ps1              — 공유 경로 변수, 유틸리티 함수, Python/conda 탐지
      modules/01_preflight    — CLI 도구 탐지 및 의존성 검증
      modules/02_interactive  — 사용자 대화형 설정 수집
      modules/03_python_env   — Python 환경 bootstrap (conda/venv)
      modules/04_dependencies — Python 패키지, 임베딩 모델, Ollama 모델
      modules/05_config       — Runtime config, User config, MCP config (전 클라이언트)
      modules/06_db           — DB 초기화, Identity, Wiki vault, Directives
      modules/07_shims        — CLI shim 파일 생성, Goose config, Copilot skill
      modules/08_env          — PATH, 환경변수, persona.user.yaml, overlay.png
      modules/09_overlay      — Overlay exe 빌드, launcher, overlay.user.yaml
      modules/10_shortcuts    — Start Menu, Startup, KG Watcher
#>

param(
    [switch]$Uninstall,
    [ValidateSet("auto", "rebuild", "clean", "skip")]
    [string]$OverlayBuildMode = "auto"
)

$ErrorActionPreference = "Stop"

# ── 공유 변수/함수/Python 탐지 로드 ───────────────────────
. "$PSScriptRoot\common.ps1"

# ── Uninstall ──────────────────────────────────────────────
if ($Uninstall) {
    Write-Host "
  Engram Uninstaller
  ────────────────" -ForegroundColor Magenta
    if (Test-Path $ShimDir) {
        Remove-Item $ShimDir -Recurse -Force
        Write-Ok "Removed: $ShimDir"
    }
    if (Test-Path $CopilotSkillDir) {
        Remove-Item $CopilotSkillDir -Recurse -Force
        Write-Ok "Removed: $CopilotSkillDir"
    }
    if (Test-Path $LegacyCopilotSkillDir) {
        Remove-Item $LegacyCopilotSkillDir -Recurse -Force
        Write-Ok "Removed legacy skill: $LegacyCopilotSkillDir"
    }
    [Environment]::SetEnvironmentVariable(("CON" + "TINUUM_DB_DIR"), $null, "User")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$ShimDir*") {
        $newPath = ($userPath -split ";" | Where-Object { $_ -ne $ShimDir }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Ok "Removed from PATH"
    }
    $StartMenuLink = Join-Path ([Environment]::GetFolderPath("Programs")) "Engram Overlay.lnk"
    if (Test-Path $StartMenuLink) { Remove-Item $StartMenuLink -Force; Write-Ok "Removed: Start Menu shortcut" }
    $StartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk"
    if (Test-Path $StartupLink) { Remove-Item $StartupLink -Force; Write-Ok "Removed: Startup shortcut" }
    # legacy VBS 정리 (이제 overlay 자식 프로세스로 관리)
    $WatcherVbsPath = Join-Path ([Environment]::GetFolderPath("Startup")) "engram-kg-watcher.vbs"
    if (Test-Path $WatcherVbsPath) { Remove-Item $WatcherVbsPath -Force; Write-Ok "Removed: $WatcherVbsPath (legacy)" }
    Write-Host "
  Done. DB and MCP config preserved.
" -ForegroundColor Green
    exit 0
}

# ── Install ────────────────────────────────────────────────
Write-Host ""
Write-Host "  Engram Installer" -ForegroundColor Magenta
Write-Host "  ───────────────────────────" -ForegroundColor Magenta
Write-Host "  Overlay build mode: $OverlayBuildMode" -ForegroundColor DarkGray
Write-Host ""

. "$PSScriptRoot\modules\01_preflight.ps1"
. "$PSScriptRoot\modules\02_interactive.ps1"
. "$PSScriptRoot\modules\03_python_env.ps1"
. "$PSScriptRoot\modules\04_dependencies.ps1"
. "$PSScriptRoot\modules\05_config.ps1"
. "$PSScriptRoot\modules\06_db.ps1"
. "$PSScriptRoot\modules\07_shims.ps1"
. "$PSScriptRoot\modules\08_env.ps1"
. "$PSScriptRoot\modules\09_overlay.ps1"
. "$PSScriptRoot\modules\10_shortcuts.ps1"

# ── Auto-launch overlay ──────────────────────────────────────
if (-not $Uninstall -and (Test-Path $DistExe)) {
    Write-Host "  Launching engram-overlay..." -ForegroundColor DarkGray
    Start-Process -FilePath $DistExe -WindowStyle Normal
}

# ── Done ───────────────────────────────────────────────────
Write-Host ""
Write-Host "  Install complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Usage:" -ForegroundColor White
Write-Host "    engram                   Start interactive session (engram identity auto-loaded)" -ForegroundColor Gray
Write-Host "    /engram                  Inject engram identity in ANY active Copilot CLI session" -ForegroundColor Gray
Write-Host "    engram -p ""prompt""       Non-interactive" -ForegroundColor Gray
Write-Host "    engram --overlay         Start with character overlay" -ForegroundColor Gray
Write-Host "    engram --overlay-stop    Stop running overlay" -ForegroundColor Gray
Write-Host "    engram-overlay           Launch overlay (standalone, kg_watcher auto-managed)" -ForegroundColor Gray
Write-Host "    .\install.ps1 -OverlayBuildMode auto|rebuild|clean|skip" -ForegroundColor Gray
Write-Host ""
Write-Host "  Default CLI provider: $DefaultCliProvider" -ForegroundColor Gray
Write-Host "  Settings: $ShimDir\overlay.user.yaml" -ForegroundColor Gray
Write-Host ""
exit 0