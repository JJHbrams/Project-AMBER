<#
.SYNOPSIS
    Engram Installer — 오케스트레이터
    Install: .\install.ps1
    Install (overlay build mode): .\install.ps1 -OverlayBuildMode auto|rebuild|clean|skip
    Install (설정 TUI 다시 보기): .\install.ps1 -Reconfigure
    Remove:  .\install.ps1 -Uninstall

    기존 설정(DB 경로/작업 디렉토리/CLI provider/Ollama 모델)이 이미 있으면
    재설치 시 대화형 TUI(화살표 선택)를 건너뛰고 기존 값을 조용히 재사용한다.
    -Reconfigure를 주면 기존 값이 있어도 항상 TUI를 다시 띄운다.

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
    [string]$OverlayBuildMode = "auto",
    [switch]$Reconfigure
)

$ErrorActionPreference = "Stop"

# ── 공유 변수/함수/Python 탐지 로드 ───────────────────────
. "$PSScriptRoot\common.ps1"

function Remove-EngramManagedClaudeHooks {
    $settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
    $markers = @("engram-sessionstart-hook", "engram-claude-pretool-hook")
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $settingsChanged = $false
    if (Test-Path $settingsPath) {
        try {
            $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "Unable to update Claude hooks in $settingsPath because it could not be parsed as JSON. Repair or remove the file, then retry uninstall. Engram-managed hook scripts were preserved."
        }
        if ($settings -isnot [pscustomobject]) {
            throw "Unable to update Claude hooks in $settingsPath because the top-level JSON value is not an object. Repair the file, then retry uninstall. Engram-managed hook scripts were preserved."
        }
        if ($settings.PSObject.Properties["hooks"] -and $settings.hooks -and $settings.hooks -isnot [pscustomobject]) {
            throw "Unable to update Claude hooks in $settingsPath because 'hooks' is not a JSON object. Repair the file, then retry uninstall. Engram-managed hook scripts were preserved."
        }
        if ($settings -and $settings.PSObject.Properties["hooks"]) {
            foreach ($eventName in @("SessionStart", "PreToolUse")) {
                $entriesRaw = $settings.hooks.$eventName
                if ($null -eq $entriesRaw) { continue }
                $kept = New-Object System.Collections.ArrayList
                foreach ($entry in @($entriesRaw)) {
                    if (-not ($entry -and $entry.PSObject.Properties["hooks"])) {
                        [void]$kept.Add($entry)
                        continue
                    }
                    $originalHooks = @($entry.hooks)
                    $filteredHooks = New-Object System.Collections.ArrayList
                    $removedHooks = $false
                    foreach ($hook in $originalHooks) {
                        $removeHook = $false
                        $command = ""
                        if ($hook -and $hook.PSObject.Properties["command"]) {
                            $command = [string]$hook.command
                        }
                        foreach ($marker in $markers) {
                            if ($command -like "*$marker*") {
                                $removeHook = $true
                                $removedHooks = $true
                                break
                            }
                        }
                        if (-not $removeHook) { [void]$filteredHooks.Add($hook) }
                    }
                    if ($filteredHooks.Count -gt 0) {
                        if ($removedHooks) {
                            $entry.hooks = @($filteredHooks)
                            $settingsChanged = $true
                        }
                        [void]$kept.Add($entry)
                    } elseif ($originalHooks.Count -gt 0) {
                        $settingsChanged = $true
                    }
                }
                if ($kept.Count -gt 0) {
                    $settings.hooks.$eventName = @($kept)
                } else {
                    if ($settings.hooks.PSObject.Properties[$eventName]) {
                        $settings.hooks.PSObject.Properties.Remove($eventName)
                        $settingsChanged = $true
                    }
                }
            }
            if ($settings.hooks.PSObject.Properties.Count -eq 0 -and $settings.PSObject.Properties["hooks"]) {
                $settings.PSObject.Properties.Remove("hooks")
                $settingsChanged = $true
            }
            if ($settingsChanged) {
                try {
                    [System.IO.File]::WriteAllText($settingsPath, (($settings | ConvertTo-Json -Depth 12) + "`n"), $Utf8NoBom)
                } catch {
                    throw "Unable to update Claude hooks in $settingsPath. Check file permissions and retry uninstall. Engram-managed hook scripts were preserved."
                }
                Write-Ok "Removed Engram-managed Claude hooks"
            }
        }
    }
}

function Remove-EngramManagedCodexHooks {
    $hooksPath = Join-Path $env:USERPROFILE ".codex\hooks.json"
    if (-not (Test-Path $hooksPath)) { return }
    try {
        $settings = Get-Content $hooksPath -Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Unable to update Codex hooks in $hooksPath because it could not be parsed as JSON. Repair or remove the file, then retry uninstall."
    }
    if ($settings -isnot [pscustomobject]) {
        throw "Unable to update Codex hooks in $hooksPath because the top-level JSON value is not an object."
    }
    if (-not ($settings.PSObject.Properties["hooks"] -and $settings.hooks.PSObject.Properties["PreToolUse"])) { return }

    $kept = New-Object System.Collections.ArrayList
    $changed = $false
    foreach ($entry in @($settings.hooks.PreToolUse)) {
        if (-not ($entry -and $entry.PSObject.Properties["hooks"])) {
            [void]$kept.Add($entry)
            continue
        }
        $filtered = New-Object System.Collections.ArrayList
        foreach ($hook in @($entry.hooks)) {
            $command = if ($hook -and $hook.PSObject.Properties["command"]) { [string]$hook.command } else { "" }
            if ($command -like "*engram-codex-pretool-hook*") {
                $changed = $true
            } else {
                [void]$filtered.Add($hook)
            }
        }
        if ($filtered.Count -gt 0) {
            $entry.hooks = @($filtered)
            [void]$kept.Add($entry)
        }
    }
    if (-not $changed) { return }
    if ($kept.Count -gt 0) {
        $settings.hooks.PreToolUse = @($kept)
    } else {
        $settings.hooks.PSObject.Properties.Remove("PreToolUse")
    }
    if ($settings.hooks.PSObject.Properties.Count -eq 0) {
        $settings.PSObject.Properties.Remove("hooks")
    }
    [System.IO.File]::WriteAllText($hooksPath, (($settings | ConvertTo-Json -Depth 12) + "`n"), [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Removed Engram-managed Codex hooks"
}

# ── Uninstall ──────────────────────────────────────────────
if ($Uninstall) {
    Write-Host "
  Engram Uninstaller
  ────────────────" -ForegroundColor Magenta
    Remove-EngramManagedClaudeHooks
    Remove-EngramManagedCodexHooks
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
    if (Test-Path $ClaudeCommandPath) {
        Remove-Item $ClaudeCommandPath -Force
        Write-Ok "Removed: $ClaudeCommandPath"
    }
    [Environment]::SetEnvironmentVariable(("CON" + "TINUUM_DB_DIR"), $null, "User")
    if ([Environment]::GetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "User") -eq $ShimDir) {
        [Environment]::SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $null, "User")
        Write-Ok "Removed COPILOT_CUSTOM_INSTRUCTIONS_DIRS"
    }
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$ShimDir*") {
        $newPath = ($userPath -split ";" | Where-Object { $_ -ne $ShimDir }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Ok "Removed from PATH"
    }
    foreach ($StartMenuLink in @(
        (Join-Path ([Environment]::GetFolderPath("Programs")) "AMBER (ENGRAM).lnk"),
        (Join-Path ([Environment]::GetFolderPath("Programs")) "Engram Overlay.lnk")
    )) {
        if (Test-Path $StartMenuLink) { Remove-Item $StartMenuLink -Force; Write-Ok "Removed: Start Menu shortcut" }
    }
    foreach ($StartupLink in @(
        (Join-Path ([Environment]::GetFolderPath("Startup")) "AMBER (ENGRAM).lnk"),
        (Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk")
    )) {
        if (Test-Path $StartupLink) { Remove-Item $StartupLink -Force; Write-Ok "Removed: Startup shortcut" }
    }
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

$installPhases = @(
    @{ Name = "Preflight checks"; Path = "modules\01_preflight.ps1" },
    @{ Name = "Interactive setup"; Path = "modules\02_interactive.ps1" },
    @{ Name = "Python environment"; Path = "modules\03_python_env.ps1" },
    @{ Name = "Dependencies"; Path = "modules\04_dependencies.ps1" },
    @{ Name = "Configuration"; Path = "modules\05_config.ps1" },
    @{ Name = "Database and identity"; Path = "modules\06_db.ps1" },
    @{ Name = "CLI shims"; Path = "modules\07_shims.ps1" },
    @{ Name = "Environment variables"; Path = "modules\08_env.ps1" },
    @{ Name = "Overlay build"; Path = "modules\09_overlay.ps1" },
    @{ Name = "Shortcuts"; Path = "modules\10_shortcuts.ps1" }
)

for ($i = 0; $i -lt $installPhases.Count; $i++) {
    $phase = $installPhases[$i]
    $idx = $i + 1
    Write-Host ("  [{0}/{1}] {2}" -f $idx, $installPhases.Count, $phase.Name) -ForegroundColor DarkCyan
    . (Join-Path $PSScriptRoot $phase.Path)
}

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
Write-Host "    .\install.ps1 -Reconfigure   설정(CLI provider 등) 다시 선택" -ForegroundColor Gray
Write-Host ""
Write-Host "  Default CLI provider: $DefaultCliProvider" -ForegroundColor Gray
Write-Host "  Settings: $ShimDir\overlay.user.yaml" -ForegroundColor Gray
Write-Host ""
exit 0
