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
    [ValidateSet("copilot", "gemini", "claude-code", "claude-code-ollama", "ollama")]
    [string]$CliProvider = "claude-code",
    [string]$OllamaModel = "",
    [string]$IdentityName = "",
    [switch]$EnableAutoStart,
    [switch]$LaunchNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

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

# ── Uninstall ───────────────────────────────────────────────
if ($Uninstall) {
    Write-Step "Uninstall — 바로가기/환경변수 정리 (DB·config 보존)"
    Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue | Stop-Process -Force
    foreach ($lnk in @(
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
    exit 0
}

if (-not (Test-Path $DistExe)) {
    Write-Warn "engram-overlay.exe 없음: $DistExe — 번들 복사가 선행돼야 합니다."
    exit 1
}
if (-not $DbDir)   { $DbDir   = if (Test-Path "D:\") { "D:\intel_engram" } else { "C:\intel_engram" } }
if (-not $WorkDir) { $WorkDir = $DbDir }
if (-not (Test-Path $DbDir)) { New-Item $DbDir -ItemType Directory -Force | Out-Null }

Write-Host ""
Write-Host "  Engram Overlay — Configure" -ForegroundColor Magenta
Write-Host "  InstallDir : $InstallDir" -ForegroundColor DarkGray
Write-Host "  DB / Work  : $DbDir / $WorkDir" -ForegroundColor DarkGray
$providerLabel = $CliProvider
if ($OllamaModel) { $providerLabel = "$CliProvider ($OllamaModel)" }
Write-Host "  Provider   : $providerLabel" -ForegroundColor DarkGray
Write-Host ""

# ── 1. user.config.yaml (템플릿, python 불필요) ──────────────
Write-Step "user.config.yaml"
if (-not (Test-Path $UserConfig)) {
    $u = @"
# User runtime overrides for Engram.
db:
  root_dir: "$($DbDir -replace '\\','/')"

workdir: "$($WorkDir -replace '\\','/')"

# watch_workspaces:
#   - C:/Users/yourname/Desktop/Workspace
"@
    [System.IO.File]::WriteAllText($UserConfig, $u, $Utf8NoBom)
    Write-Ok "Created: $UserConfig"
} else {
    Write-Ok "Exists (보존): $UserConfig"
}

# ── 2. overlay.user.yaml (provider/모델/mcp 포트) ────────────
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
    # 기존 보존, provider/포트만 라인 치환은 생략 — 이미 있으면 사용자 설정 존중
    Write-Ok "Exists (보존): $OverlayConfig"
}

# ── 3. MCP 설정 (JSON, PowerShell 네이티브) ──────────────────
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
# Gemini CLI (있을 때만)
if (Get-Command gemini -ErrorAction SilentlyContinue) {
    & gemini mcp remove --scope user engram *> $null
    & gemini mcp add --scope user --transport http engram $mcpUrl *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "Gemini CLI" } else { Write-Warn "Gemini MCP 등록 실패 (수동: gemini mcp add --scope user --transport http engram $mcpUrl)" }
}

# ── 4. .env 템플릿 ───────────────────────────────────────────
if (-not (Test-Path $EnvFile)) {
    [System.IO.File]::WriteAllText($EnvFile, "# Engram 환경변수`n# Discord Bot Token (선택)`nDISCORD_BOT_TOKEN=`n", $Utf8NoBom)
    Write-Ok ".env 템플릿 생성"
}

# ── 5. 환경변수 (User 영구) ──────────────────────────────────
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

# ── 6. DB / Wiki / Directives bootstrap ─────────────────────
Write-Step "DB / Wiki / Directives 초기화"
& $DistExe --role install-bootstrap --db-dir $DbDir --templates-dir $InstallerTemplates
if ($LASTEXITCODE -ne 0) {
    Write-Warn "초기화 실패 (exit=$LASTEXITCODE)"
    exit 1
}
Write-Ok "DB schema, wiki starter files, directives"

# ── 7. Copilot / Claude skills ──────────────────────────────
Write-Step "Engram workflow skills"
$sharedSkillNames = @("engram-new-session", "engram-task-workflow", "engram-wiki-workflow", "engram-close-session")
foreach ($skillName in $sharedSkillNames) {
    $skillSrc = Join-Path $WorkflowSkillsSource "$skillName\SKILL.md"
    if (-not (Test-Path $skillSrc)) {
        Write-Warn "Skill source 없음: $skillSrc"
        continue
    }
    foreach ($skillRoot in @(
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
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Engram Overlay.lnk"
New-Lnk $startMenu "Engram Overlay"
Write-Ok $startMenu
$startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk"
if ($EnableAutoStart) {
    New-Lnk $startupLnk "Engram Overlay — Auto Start"
    Write-Ok "자동시작 등록: $startupLnk"
} elseif (Test-Path $startupLnk) {
    Remove-Item $startupLnk -Force
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
exit 0
