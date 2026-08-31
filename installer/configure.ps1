<#
.SYNOPSIS
    Engram Overlay — 설치타임 경량 구성기 (frozen/통짜 installer 전용)

    네이티브 setup.exe(Inno Setup)가 frozen 번들을 복사한 뒤 호출한다.
    conda/pip/PyInstaller 없이 config·MCP·환경변수·바로가기만 처리한다(순수 PowerShell).

    모든 옵션은 파라미터로 받아 완전 무인 동작한다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File configure.ps1 `
        -InstallDir "C:\Program Files\EngramOverlay" `
        -DbDir "D:\intel_engram" -WorkDir "D:\intel_engram" `
        -CliProvider claude-code -EnableAutoStart -LaunchNow
#>
param(
    [Parameter(Mandatory)][string]$InstallDir,
    [string]$DbDir = "",
    [string]$WorkDir = "",
    [ValidateSet("copilot", "antigravity", "gemini", "codex", "claude-code", "claude-code-ollama", "ollama")]
    [string]$CliProvider = "claude-code",
    [string]$OllamaModel = "",
    [string]$IdentityName = "",
    [switch]$EnableAutoStart,
    [switch]$LaunchNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

# One-release non-interactive compatibility only; the GUI and docs expose
# Antigravity exclusively.
if ($CliProvider -eq "gemini") {
    Write-Warn "Deprecated -CliProvider gemini mapped to antigravity."
    $CliProvider = "antigravity"
}

function Write-Step($m) { Write-Host "  [+] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }

# ── 경로 ────────────────────────────────────────────────────
$ShimDir       = Join-Path $env:USERPROFILE ".engram"
$DistExe       = Join-Path $InstallDir "dist\engram-overlay\engram-overlay.exe"
$UserConfig    = Join-Path $ShimDir "user.config.yaml"
$OverlayConfig = Join-Path $ShimDir "overlay.user.yaml"
$CopilotInstructions = Join-Path $ShimDir "copilot-instructions.md"
$CopilotInstructionsSource = Join-Path $InstallDir "config\clients\copilot.md"
$InstallerTemplates = Join-Path $InstallDir "installer\templates"
$WorkflowSkillsSource = Join-Path $InstallDir ".github\skills"
$EnvFile       = Join-Path $ShimDir ".env"
$MCP_HTTP_PORT = 17385
$Utf8NoBom     = [System.Text.UTF8Encoding]::new($false)

if (-not (Test-Path $ShimDir)) { New-Item $ShimDir -ItemType Directory -Force | Out-Null }
$ConfigureLogDir = Join-Path $ShimDir "logs"
if (-not (Test-Path $ConfigureLogDir)) { New-Item $ConfigureLogDir -ItemType Directory -Force | Out-Null }
$ConfigureLog = Join-Path $ConfigureLogDir ("configure-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
Start-Transcript -Path $ConfigureLog -Force | Out-Null
$script:ConfigureTranscriptStarted = $true
Write-Host "  Configure log: $ConfigureLog" -ForegroundColor DarkGray

function Stop-ConfigureTranscriptSafely {
    if (-not $script:ConfigureTranscriptStarted) { return }
    try {
        Stop-Transcript | Out-Null
    } catch {
        # Logging cleanup must never replace the installer's original result.
        try { Write-Host "  [!] Configure log close failed: $_" -ForegroundColor Yellow } catch {}
    } finally {
        $script:ConfigureTranscriptStarted = $false
    }
}

function Exit-Configure([int]$Code) {
    Stop-ConfigureTranscriptSafely
    exit $Code
}

function Invoke-EngramFrozenRole {
    param(
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string[]]$ArgumentList
    )

    $process = Start-Process `
        -FilePath $DistExe `
        -ArgumentList (@("--role", $Role) + $ArgumentList) `
        -WorkingDirectory (Split-Path $DistExe) `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    return $process.ExitCode
}

function Remove-EngramManagedClaudeHooks {
    $settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
    $markers = @("engram-sessionstart-hook", "engram-claude-pretool-hook")
    $scripts = @(
        (Join-Path $ShimDir "engram-sessionstart-hook.ps1"),
        (Join-Path $ShimDir "engram-claude-pretool-hook.ps1")
    )

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

    foreach ($scriptPath in $scripts) {
        if (Test-Path $scriptPath) {
            Remove-Item $scriptPath -Force
            Write-Ok "Removed: $scriptPath"
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

# ── Uninstall / install ─────────────────────────────────────
# The finally block also closes the transcript for unexpected terminating
# errors. Explicit outcomes use Exit-Configure so their original exit code is
# retained even if transcript cleanup itself fails.
try {
if ($Uninstall) {
    Write-Step "Uninstall — 바로가기/환경변수 정리 (DB·config 보존)"
    Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-EngramManagedClaudeHooks
    Remove-EngramManagedCodexHooks
    foreach ($lnk in @(
        (Join-Path ([Environment]::GetFolderPath("Programs")) "AMBER (ENGRAM).lnk"),
        (Join-Path ([Environment]::GetFolderPath("Startup")) "AMBER (ENGRAM).lnk"),
        (Join-Path ([Environment]::GetFolderPath("Programs")) "Engram Overlay.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk")
    )) { if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Ok "Removed: $lnk" } }
    foreach ($v in @("ENGRAM_DB_DIR", "ENGRAM_WORKDIR", "ENGRAM_PROJECT_ROOT")) {
        [Environment]::SetEnvironmentVariable($v, $null, "User")
    }
    if ([Environment]::GetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "User") -eq $ShimDir) {
        [Environment]::SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $null, "User")
    }
    Write-Ok "Done (DB/config preserved at $ShimDir)"
    Exit-Configure 0
}

if (-not (Test-Path $DistExe)) {
    Write-Warn "engram-overlay.exe 없음: $DistExe — 번들 복사가 선행돼야 합니다."
    Exit-Configure 1
}
if (-not $DbDir)   { $DbDir   = if (Test-Path "D:\") { "D:\intel_engram" } else { "C:\intel_engram" } }
if (-not $WorkDir) { $WorkDir = $DbDir }
if (-not (Test-Path $DbDir)) { New-Item $DbDir -ItemType Directory -Force | Out-Null }

Write-Host ""
Write-Host "  AMBER (ENGRAM) — Configure" -ForegroundColor Magenta
Write-Host "  InstallDir : $InstallDir" -ForegroundColor DarkGray
Write-Host "  DB / Work  : $DbDir / $WorkDir" -ForegroundColor DarkGray
$providerLabel = $CliProvider
if ($OllamaModel) { $providerLabel = "$CliProvider ($OllamaModel)" }
Write-Host "  Provider   : $providerLabel" -ForegroundColor DarkGray
Write-Host ""

# ── 1. DB / Wiki / Directives bootstrap ─────────────────────
# Keep this before config migration and external CLI/MCP registration.  A
# legacy user may have a valid selected DB/wiki directory but malformed client
# config; managed manuals must still be repaired before those fallible steps.
Write-Step "DB / Wiki / Directives 초기화"
$installBootstrapArgs = @(
    "--db-dir", ('"{0}"' -f $DbDir),
    "--templates-dir", ('"{0}"' -f $InstallerTemplates)
)
$installBootstrapExitCode = Invoke-EngramFrozenRole `
    -Role "install-bootstrap" `
    -ArgumentList $installBootstrapArgs
if ($installBootstrapExitCode -ne 0) {
    Write-Warn "초기화 실패 (exit=$installBootstrapExitCode, log=$ConfigureLog)"
    Exit-Configure 1
}
Write-Ok "DB schema, wiki starter files, directives"

# ── 2. user.config.yaml (템플릿, python 불필요) ──────────────
Write-Step "user.config.yaml"
if (-not (Test-Path $UserConfig)) {
    $u = @"
# User runtime overrides for Engram.
db:
  root_dir: "$($DbDir -replace '\\','/')"

workdir: "$($WorkDir -replace '\\','/')"

# Optional external human-readable daily note folder.
# memory:
#   auto_checkpoint:
#     external_daily_dir: "D:/Notes/daily"

# watch_workspaces:
#   - C:/Users/yourname/Desktop/Workspace
"@
    [System.IO.File]::WriteAllText($UserConfig, $u, $Utf8NoBom)
    Write-Ok "Created: $UserConfig"
} else {
    $installUserConfigArgs = @(
        "--config-path", ('"{0}"' -f $UserConfig),
        "--db-dir", ('"{0}"' -f $DbDir),
        "--workdir", ('"{0}"' -f $WorkDir)
    )
    $installUserConfigExitCode = Invoke-EngramFrozenRole `
        -Role "install-user-config" `
        -ArgumentList $installUserConfigArgs
    if ($installUserConfigExitCode -ne 0) {
        Write-Warn "사용자 설정 경로 갱신 실패 (exit=$installUserConfigExitCode)"
        Exit-Configure 1
    }
    Write-Ok "Updated: $UserConfig (db.root_dir, workdir)"
}

# ── 3. overlay.user.yaml (provider/모델/mcp 포트) ────────────
Write-Step "overlay.user.yaml"
$ollamaLine = if ($OllamaModel) { "  ollama_model: `"$OllamaModel`"`n" } else { "" }
$o = @"
cli:
  provider: "$CliProvider"
$ollamaLine
mcp:
  http_port: $MCP_HTTP_PORT
"@
if (-not (Test-Path $OverlayConfig)) {
    [System.IO.File]::WriteAllText($OverlayConfig, $o, $Utf8NoBom)
    Write-Ok "Created: $OverlayConfig"
} else {
    $overlayConfigArgs = @("--config-path", ('"{0}"' -f $OverlayConfig), "--overlay-provider", $CliProvider, "--overlay-mcp-port", $MCP_HTTP_PORT)
    if ($OllamaModel) { $overlayConfigArgs += @("--overlay-ollama-model", $OllamaModel) }
    $overlayConfigExitCode = Invoke-EngramFrozenRole -Role "install-user-config" -ArgumentList $overlayConfigArgs
    if ($overlayConfigExitCode -ne 0) { Write-Warn "overlay.user.yaml provider 갱신 실패 (exit=$overlayConfigExitCode)"; Exit-Configure 1 }
    Write-Ok "Updated: $OverlayConfig (cli.provider, mcp.http_port)"
}

# ── 4. MCP 설정 (JSON, PowerShell 네이티브) ──────────────────
# 모든 클라이언트가 overlay 수명 공유 HTTP 서버(127.0.0.1:$MCP_HTTP_PORT)로 접속.
Write-Step "MCP 설정 (HTTP)"
$mcpUrl = "http://127.0.0.1:$MCP_HTTP_PORT/mcp"

function Merge-JsonMcp {
    param([string]$Path, [string]$ServersKey, [hashtable]$Entry)
    try {
        $dir = Split-Path $Path
        if ($dir -and -not (Test-Path $dir)) { New-Item $dir -ItemType Directory -Force | Out-Null }
        $root = if (Test-Path $Path) {
            try { Get-Content $Path -Raw | ConvertFrom-Json } catch { [PSCustomObject]@{} }
        } else { [PSCustomObject]@{} }
        if (-not $root.PSObject.Properties[$ServersKey]) {
            $root | Add-Member -NotePropertyName $ServersKey -NotePropertyValue ([PSCustomObject]@{}) -Force
        }
        foreach ($legacy in @("continuum")) {
            if ($root.$ServersKey.PSObject.Properties[$legacy]) { $root.$ServersKey.PSObject.Properties.Remove($legacy) }
        }
        if ($root.$ServersKey.PSObject.Properties["engram"]) { $root.$ServersKey.PSObject.Properties.Remove("engram") }
        $root.$ServersKey | Add-Member -NotePropertyName engram -NotePropertyValue ([PSCustomObject]$Entry) -Force
        [System.IO.File]::WriteAllText($Path, ($root | ConvertTo-Json -Depth 12), $Utf8NoBom)
        return $true
    } catch {
        Write-Warn "MCP 설정 실패 ($Path): $_"
        return $false
    }
}

$httpEntry = @{ type = "http"; url = $mcpUrl }
# Copilot CLI (~/.copilot/mcp-config.json) : mcpServers
if (Merge-JsonMcp -Path (Join-Path $env:USERPROFILE ".copilot\mcp-config.json") -ServersKey "mcpServers" -Entry $httpEntry) { Write-Ok "Copilot CLI" }
# Claude Code (~/.claude.json) : mcpServers
if (Merge-JsonMcp -Path (Join-Path $env:USERPROFILE ".claude.json") -ServersKey "mcpServers" -Entry $httpEntry) { Write-Ok "Claude Code" }
# ~/.engram/claude-mcp.json
[System.IO.File]::WriteAllText((Join-Path $ShimDir "claude-mcp.json"), (@{ mcpServers = @{ engram = $httpEntry } } | ConvertTo-Json -Depth 6), $Utf8NoBom)
# VSCode global (%APPDATA%/Code/User/mcp.json) : servers
if (Merge-JsonMcp -Path (Join-Path $env:APPDATA "Code\User\mcp.json") -ServersKey "servers" -Entry $httpEntry) { Write-Ok "VSCode (global)" }
# Antigravity CLI (official AGY user MCP command)
if (Get-Command agy -ErrorAction SilentlyContinue) {
    & agy mcp add engram $mcpUrl *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Antigravity (agy)" } else { Write-Warn "Antigravity MCP 등록 실패 (수동: agy mcp add engram $mcpUrl)" }
}
# Codex CLI (기존 사용자 정의 engram 항목은 보존)
if (Get-Command codex -ErrorAction SilentlyContinue) {
    & codex mcp get engram *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Codex CLI (existing engram MCP preserved)"
    } else {
        & codex mcp add engram --url $mcpUrl *> $null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Codex CLI" } else { Write-Warn "Codex MCP 등록 실패 (수동: codex mcp add engram --url $mcpUrl)" }
    }
}

# ── 5. .env 템플릿 ───────────────────────────────────────────
if (-not (Test-Path $EnvFile)) {
    [System.IO.File]::WriteAllText($EnvFile, "# Engram 환경변수`n# Discord Bot Token (선택)`nDISCORD_BOT_TOKEN=`n", $Utf8NoBom)
    Write-Ok ".env 템플릿 생성"
}

# ── 6. 환경변수 (User 영구) ──────────────────────────────────
Write-Step "환경변수 (User)"
[Environment]::SetEnvironmentVariable("ENGRAM_DB_DIR", $DbDir, "User")
[Environment]::SetEnvironmentVariable("ENGRAM_WORKDIR", $WorkDir, "User")
[Environment]::SetEnvironmentVariable("ENGRAM_PROJECT_ROOT", $InstallDir, "User")
[Environment]::SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")
[Environment]::SetEnvironmentVariable(("CON" + "TINUUM_DB_DIR"), $null, "User")
Write-Ok "ENGRAM_DB_DIR / ENGRAM_WORKDIR / ENGRAM_PROJECT_ROOT / COPILOT_CUSTOM_INSTRUCTIONS_DIRS"

if (Test-Path $CopilotInstructionsSource) {
    Copy-Item $CopilotInstructionsSource $CopilotInstructions -Force
    Write-Ok "Copilot session protocol: $CopilotInstructions"
} else {
    Write-Warn "Copilot session protocol source 없음: $CopilotInstructionsSource"
}

# ── 7. Copilot / Claude skills ──────────────────────────────
Write-Step "Engram workflow skills"
$sharedSkillNames = @("orchestrate", "engram-new-session", "engram-task-workflow", "engram-wiki-workflow", "engram-close-session")
foreach ($skillName in $sharedSkillNames) {
    $skillSrc = Join-Path $WorkflowSkillsSource "$skillName\SKILL.md"
    if (-not (Test-Path $skillSrc)) {
        Write-Warn "Skill source 없음: $skillSrc"
        continue
    }
    foreach ($skillRoot in @(
        (Join-Path $env:USERPROFILE ".agents\skills"),
        (Join-Path $env:USERPROFILE ".claude\skills"),
        (Join-Path $env:USERPROFILE ".copilot\skills")
    )) {
        $skillDir = Join-Path $skillRoot $skillName
        if (-not (Test-Path $skillDir)) { New-Item $skillDir -ItemType Directory -Force | Out-Null }
        Copy-Item $skillSrc (Join-Path $skillDir "SKILL.md") -Force
        Write-Ok (Join-Path $skillDir "SKILL.md")
    }
}
$copilotEngramSkill = Join-Path $WorkflowSkillsSource "engram\SKILL.md"
if (Test-Path $copilotEngramSkill) {
    $skillDir = Join-Path $env:USERPROFILE ".copilot\skills\engram"
    if (-not (Test-Path $skillDir)) { New-Item $skillDir -ItemType Directory -Force | Out-Null }
    Copy-Item $copilotEngramSkill (Join-Path $skillDir "SKILL.md") -Force
    Write-Ok (Join-Path $skillDir "SKILL.md")
} else {
    Write-Warn "Skill source 없음: $copilotEngramSkill"
}
Write-Warn "이미 열려 있던 CLI는 새 환경변수와 skill 목록을 다시 읽지 않습니다. 터미널과 CLI 세션을 새로 시작하세요."

# ── 8. Identity 이름 (선택) — 첫 실행 시 반영되도록 env 로 전달 ──
if ($IdentityName) {
    [Environment]::SetEnvironmentVariable("ENGRAM_INSTALL_NAME", $IdentityName, "User")
    Write-Ok "Identity: $IdentityName (첫 실행 시 적용)"
}

# ── 9. 바로가기 (exe 직접 타깃 → 작업표시줄 고정 가능) ────────
Write-Step "바로가기"
$exeDir = Split-Path $DistExe
$shell = New-Object -ComObject WScript.Shell
function New-Lnk($path, $desc) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $DistExe
    $s.WorkingDirectory = $exeDir
    $s.Description = $desc
    $s.IconLocation = "$DistExe,0"
    $s.Save()
}
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "AMBER (ENGRAM).lnk"
$legacyStartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Engram Overlay.lnk"
New-Lnk $startMenu "AMBER (ENGRAM)"
if (Test-Path $legacyStartMenu) { Remove-Item $legacyStartMenu -Force }
Write-Ok $startMenu
$startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "AMBER (ENGRAM).lnk"
$legacyStartupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk"
if ($EnableAutoStart) {
    New-Lnk $startupLnk "AMBER (ENGRAM) — Auto Start"
    if (Test-Path $legacyStartupLnk) { Remove-Item $legacyStartupLnk -Force }
    Write-Ok "자동시작 등록: $startupLnk"
} else {
    foreach ($link in @($startupLnk, $legacyStartupLnk)) {
        if (Test-Path $link) { Remove-Item $link -Force }
    }
    Write-Ok "자동시작 해제"
}

# ── 10. 실행 ─────────────────────────────────────────────────
if ($LaunchNow) {
    Write-Step "engram-overlay 실행"
    Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 800
    Start-Process -FilePath $DistExe -WorkingDirectory $exeDir
    Write-Ok "실행됨"
}

Write-Host ""
Write-Host "  Configure 완료." -ForegroundColor Green
Write-Host ""
Exit-Configure 0
} finally {
    Stop-ConfigureTranscriptSafely
}
