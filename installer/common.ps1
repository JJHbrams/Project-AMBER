#
# common.ps1 — 공유 경로 변수, 유틸리티 함수, Python/conda 탐지
# install.ps1 에서 dot-source 로 로드됩니다.
#

# UTF-8 출력 (한글 깨짐 방지)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

# ── Paths ──────────────────────────────────────────────────
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ShimDir = Join-Path $env:USERPROFILE ".engram"
$ShimPath = Join-Path $ShimDir "engram-copilot.cmd"
$EngramDispatcherPath = Join-Path $ShimDir "engram.cmd"
$LegacyCopilotShimPath = Join-Path $ShimDir "engram.cmd"  # 현재는 dispatcher로 재사용
$GeminiShimPath = Join-Path $ShimDir "engram-gemini.cmd"
$ClaudeShimPath = Join-Path $ShimDir "engram-claude.cmd"
$GooseShimPath  = Join-Path $ShimDir "engram-goose.cmd"
$ClaudeMcpConfigPath = Join-Path $ShimDir "claude-mcp.json"
$CopilotInstructionsPath = Join-Path $ShimDir "copilot-instructions.md"
$GooseExePath   = Join-Path $ShimDir "goose.exe"
$GooseConfigDir = Join-Path $env:APPDATA "Block\goose\config"
$GooseConfigPath = Join-Path $GooseConfigDir "config.yaml"
$GooseLegacyConfigDir = Join-Path $env:USERPROFILE ".config\goose"
$GooseLegacyConfigPath = Join-Path $GooseLegacyConfigDir "config.yaml"
$UserConfigPath = Join-Path $ShimDir "user.config.yaml"
$OverlayUserConfigPath = Join-Path $ShimDir "overlay.user.yaml"
$LegacyShimPath = Join-Path $ShimDir ("con" + "tinuum.cmd")
$CopilotSkillDir = Join-Path $env:USERPROFILE ".copilot\skills\engram"
$CopilotSkillPath = Join-Path $CopilotSkillDir "SKILL.md"
$LegacyCopilotSkillDir = Join-Path $env:USERPROFILE (".copilot\\skills\\" + ("con" + "tinuum"))
$McpConfigPath = Join-Path $env:USERPROFILE ".copilot\mcp-config.json"
$ClaudeConfigPath = Join-Path $env:USERPROFILE ".claude.json"
$RuntimeConfigPath = Join-Path $ProjectRoot "config\config.yaml"
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$EnvironmentYamlPath = Join-Path $ProjectRoot "environment.yml"
$ProjectVenvDir = Join-Path $ProjectRoot ".venv"
$CondaEnv = "intel_engram"
$McpServerScript = Join-Path $ProjectRoot "mcp_server.py"
$MCP_HTTP_PORT = 17385  # Copilot/Gemini CLI용 지속 MCP HTTP(SSE) 서버 포트
$HasNamedCondaEnv = $false

# ── conda Python 동적 탐지 ──────────────────────────────────
$PythonExe = $null

# 방법 1: conda info로 envs 목록에서 탐색
$condaCmd = Get-Command conda -ErrorAction SilentlyContinue
if ($condaCmd) {
    $envPath = (conda info --envs 2>&1 | Select-String "^\s*$CondaEnv\s") -replace "^\s*$CondaEnv\s+\*?\s*", "" | ForEach-Object { $_.Trim() }
    if ($envPath) {
        $candidate = Join-Path $envPath "python.exe"
        if (Test-Path $candidate) { $PythonExe = $candidate; $HasNamedCondaEnv = $true }
    }
}

# 방법 2: 일반적인 conda 설치 경로 순서대로 탐색
if (-not $PythonExe) {
    $candidates = @(
        "$env:USERPROFILE\miniconda3\envs\$CondaEnv\python.exe",
        "$env:USERPROFILE\anaconda3\envs\$CondaEnv\python.exe",
        "$env:LOCALAPPDATA\miniconda3\envs\$CondaEnv\python.exe",
        "C:\miniconda3\envs\$CondaEnv\python.exe",
        "C:\anaconda3\envs\$CondaEnv\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonExe = $c; $HasNamedCondaEnv = $true; break }
    }
}

if (-not $PythonExe) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and (Test-Path $pythonCmd.Source)) {
        $PythonExe = $pythonCmd.Source
    }
}

# DB 경로 기본값 (설치 중 사용자 입력으로 재정의됨)
$DefaultDbDir = if (Test-Path "D:\") { "D:\intel_engram" } else { "C:\intel_engram" }

# ── Utility functions ──────────────────────────────────────
function Write-Step($msg) { Write-Host "  [+] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  [X] $msg" -ForegroundColor Red }

# Runs $ScriptBlock, printing each output line truncated to a single overwriting
# console line so the user sees progress. Returns a List<string> of all lines
# for error reporting. $LASTEXITCODE reflects the native command's exit code.
function Invoke-LiveLog([scriptblock]$ScriptBlock) {
    $spinner = @('|', '/', '-', '\')
    $spinIdx  = 0
    $lines    = [System.Collections.Generic.List[string]]::new()
    & $ScriptBlock 2>&1 | ForEach-Object {
        $line = $_.ToString().Trim()
        if ($line) {
            $lines.Add($line)
            $disp = if ($line.Length -gt 70) { $line.Substring(0, 67) + "..." } else { $line }
            $frame = $spinner[$spinIdx % $spinner.Length]
            $spinIdx++
            Write-Host -NoNewline "`r    $frame $($disp.PadRight(75))"
        }
    }
    if ($lines.Count -gt 0) { Write-Host "" }
    return ,$lines
}

function Invoke-PythonScriptText {
    param(
        [Parameter(Mandatory)][string]$PythonPath,
        [Parameter(Mandatory)][string]$ScriptText
    )

    $tempPy = Join-Path ([System.IO.Path]::GetTempPath()) ("engram-install-" + [System.Guid]::NewGuid().ToString("N") + ".py")
    [System.IO.File]::WriteAllText($tempPy, $ScriptText, [System.Text.UTF8Encoding]::new($false))
    try {
        return & $PythonPath $tempPy 2>&1
    }
    finally {
        Remove-Item -Path $tempPy -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-CondaEnvPythonPath([string]$envName) {
    if (-not $condaCmd) {
        return $null
    }
    $pattern = "^\s*$([regex]::Escape($envName))\s"
    $line = conda info --envs 2>&1 | Select-String $pattern | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $envPath = ($line.ToString()) -replace "^\s*$([regex]::Escape($envName))\s+\*?\s*", ""
    $envPath = $envPath.Trim()
    if (-not $envPath) {
        return $null
    }
    $candidate = Join-Path $envPath "python.exe"
    if (Test-Path $candidate) {
        return $candidate
    }
    return $null
}

function Normalize-CliProvider([string]$provider) {
    $value = ("$provider").Trim().ToLower()
    switch ($value) {
        "copilot" { return "copilot" }
        "gemini" { return "gemini" }
        "claude" { return "claude-code" }
        "claude-code" { return "claude-code" }
        "claude_code" { return "claude-code" }
        "claudecode" { return "claude-code" }
        "ollama" { return "ollama" }
        default { return "gemini" }
    }
}

function Resolve-AvailableCliProvider([string]$preferred, [hashtable]$availability) {
    $normalized = Normalize-CliProvider $preferred
    if ($availability.ContainsKey($normalized) -and [bool]$availability[$normalized]) {
        return $normalized
    }
    foreach ($candidate in @("gemini", "claude-code", "ollama", "copilot")) {
        if ($availability.ContainsKey($candidate) -and [bool]$availability[$candidate]) {
            return $candidate
        }
    }
    return "gemini"
}

function Write-DepStatus([string]$name, [bool]$available, [bool]$required, [string]$hint) {
    if ($available) {
        Write-Ok "$name"
        return
    }
    if ($required) {
        Write-Err "$name (missing)"
    } else {
        Write-Warn "$name (missing)"
    }
    if ($hint) {
        Write-Host "      -> $hint" -ForegroundColor DarkGray
    }
}

function Get-LatestWriteTimeUtc([string[]]$paths) {
    $latest = [DateTime]::MinValue
    foreach ($path in $paths) {
        if (-not $path -or -not (Test-Path $path)) {
            continue
        }

        $item = Get-Item $path -ErrorAction SilentlyContinue
        if (-not $item) {
            continue
        }

        if (-not $item.PSIsContainer) {
            if ($item.LastWriteTimeUtc -gt $latest) {
                $latest = $item.LastWriteTimeUtc
            }
            continue
        }

        $dirLatest = Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($dirLatest -and $dirLatest.LastWriteTimeUtc -gt $latest) {
            $latest = $dirLatest.LastWriteTimeUtc
        }
    }
    return $latest
}

function Test-OverlayBuildRequired([string]$projectRoot, [string]$specPath, [string]$distExePath) {
    if (-not (Test-Path $distExePath)) {
        return @{ Required = $true; Reason = "dist exe missing" }
    }

    $distTime = (Get-Item $distExePath).LastWriteTimeUtc
    $inputs = @(
        $specPath,
        (Join-Path $projectRoot "engram_overlay_entry.py"),
        (Join-Path $projectRoot "overlay"),
        (Join-Path $projectRoot "core"),
        (Join-Path $projectRoot "discord_bot"),
        (Join-Path $projectRoot "resource"),
        (Join-Path $projectRoot "config\overlay.yaml"),
        (Join-Path $projectRoot "config\overlay.user.yaml")
    )
    $latestSourceTime = Get-LatestWriteTimeUtc $inputs
    if ($latestSourceTime -gt $distTime) {
        return @{ Required = $true; Reason = "overlay sources changed" }
    }

    return @{ Required = $false; Reason = "dist exe up-to-date" }
}

# Goose CLI Windows 바이너리 자동 다운로드
function Install-GooseCli {
    param([string]$InstallDir)
    $gooseExe = Join-Path $InstallDir "goose.exe"
    $zipUrl = "https://github.com/block/goose/releases/latest/download/goose-x86_64-pc-windows-msvc.zip"
    $zipPath = Join-Path $env:TEMP "goose-windows-msvc.zip"
    $extractDir = Join-Path $env:TEMP "goose-extract-tmp"

    Write-Host "  Goose CLI 다운로드 중 (약 67MB)..." -ForegroundColor White
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Warn "다운로드 실패: $_"
        return $null
    }

    if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
    try {
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
    } catch {
        Write-Warn "압축 해제 실패: $_"
        return $null
    }

    $extracted = Get-ChildItem $extractDir -Filter "goose.exe" -Recurse | Select-Object -First 1
    if (-not $extracted) {
        Write-Warn "goose.exe를 압축 파일에서 찾을 수 없습니다."
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        return $null
    }

    Copy-Item $extracted.FullName $gooseExe -Force
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "goose.exe 설치 완료: $gooseExe"
    return $gooseExe
}

# 설치된 ollama 모델 목록 반환 (이름만, 헤더 제외)
function Get-OllamaModelInfoList {
    # ollama list 로 이름+크기 수집, ollama show 로 tools/ctx 보강
    $lines = & ollama list 2>&1
    $models = [System.Collections.Generic.List[PSCustomObject]]::new()
    $isFirst = $true
    foreach ($line in $lines) {
        $t = $line.ToString().Trim()
        if (-not $t) { continue }
        if ($isFirst) { $isFirst = $false; continue }
        $name = ($t -split '\s+')[0]
        if (-not $name) { continue }
        $sizeMatch = [regex]::Match($t, '(\d+\.?\d*)\s*(GB|MB)', 'IgnoreCase')
        $sizeStr = if ($sizeMatch.Success) { "$($sizeMatch.Groups[1].Value) $($sizeMatch.Groups[2].Value.ToUpper())" } else { "?" }
        $models.Add([PSCustomObject]@{ Name = $name; Size = $sizeStr; Tools = $false; CtxLen = 0 })
    }

    if ($models.Count -eq 0) { return @() }

    Write-Host "  모델 정보 조회 중..." -ForegroundColor DarkGray
    foreach ($m in $models) {
        try {
            $showLines = & ollama show $m.Name 2>&1
            $inCaps = $false; $inModel = $false
            foreach ($raw in $showLines) {
                $line = $raw.ToString().TrimEnd()
                $stripped = $line.Trim().ToLower()
                if (-not $stripped) { continue }
                $is2 = $line.StartsWith("  ") -and -not $line.StartsWith("    ")
                $is4 = $line.StartsWith("    ")
                if ($is2) {
                    $inCaps  = ($stripped -eq "capabilities")
                    $inModel = ($stripped -eq "model")
                    continue
                }
                if ($is4) {
                    if ($inCaps  -and $stripped -match '^tools') { $m.Tools = $true }
                    if ($inModel -and $stripped -match '^context length\s+(\d+)') { $m.CtxLen = [int]$Matches[1] }
                }
            }
        } catch { }
    }
    # 로딩 메시지 지우기
    $esc = [char]27
    Write-Host -NoNewline "${esc}[1A${esc}[2K"
    return ,$models.ToArray()
}

function Format-OllamaModelBadge {
    param([PSCustomObject]$Info)
    $toolsTag = if ($Info.Tools) { [char]0x2713 + "tools" } else { [char]0x2717 + "tools" }
    $ctxTag = ""
    if ($Info.CtxLen -gt 0) {
        $k = [math]::Round($Info.CtxLen / 1000)
        $ctxTag = if ($k -ge 1000) { "ctx:$([math]::Round($k/1000))M" } else { "ctx:${k}K" }
    }
    $parts = @($toolsTag, $Info.Size) + @(if ($ctxTag) { $ctxTag })
    return $parts -join " | "
}

# 하위 호환 wrapper (기존 호출부가 없어 실질적으로 미사용)
function Get-OllamaModelList {
    (Get-OllamaModelInfoList) | ForEach-Object { $_.Name }
}

# Arrow-key interactive single-select menu
# Returns: selected string value from $Items
function Select-WithArrowKeys {
    param(
        [Parameter(Mandatory)][string[]]$Items,
        [string[]]$Badges = @(),   # per-item status badge (e.g. "installed", "missing")
        [int]$DefaultIndex = 0,
        [string]$Prompt = "선택"
    )

    $selected = [math]::Max(0, [math]::Min($DefaultIndex, $Items.Count - 1))
    $lineCount = $Items.Count

    # Helper: draw all rows
    function Draw-Rows([int]$sel) {
        for ($i = 0; $i -lt $Items.Count; $i++) {
            $badge = if ($i -lt $Badges.Count) { "  ($($Badges[$i]))" } else { "" }
            if ($i -eq $sel) {
                Write-Host "  > $($Items[$i])$badge" -ForegroundColor Cyan
            } else {
                Write-Host "    $($Items[$i])$badge" -ForegroundColor DarkGray
            }
        }
    }

    Write-Host "  $Prompt  (↑↓ 이동, Enter 선택)" -ForegroundColor White
    Draw-Rows $selected

    while ($true) {
        $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

        $prevSelected = $selected
        switch ($key.VirtualKeyCode) {
            38 { if ($selected -gt 0) { $selected-- } }                    # Up
            40 { if ($selected -lt ($Items.Count - 1)) { $selected++ } }   # Down
            13 { break }                                                     # Enter
        }

        if ($key.VirtualKeyCode -eq 13) { break }

        if ($selected -ne $prevSelected) {
            # Move cursor up $lineCount lines and redraw
            $esc = [char]27
            Write-Host -NoNewline "${esc}[$($lineCount)A"
            Draw-Rows $selected
        }
    }

    return $Items[$selected]
}
