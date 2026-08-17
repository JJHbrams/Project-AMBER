#
# 09_overlay.ps1 — Overlay exe 빌드, launcher cmd, overlay.user.yaml, .env 템플릿
#

# 9. Overlay exe
Write-Step "Overlay build..."
$DistExe = Join-Path $ProjectRoot "dist\engram-overlay\engram-overlay.exe"
$overlayEngine = Join-Path $ProjectRoot "installer\build-overlay.ps1"

if (-not (Test-Path $overlayEngine)) {
    Write-Warn "Overlay build engine not found — skipping overlay build"
} else {
    $engineArguments = @{
        Mode = $OverlayBuildMode
        CondaEnv = $CondaEnv
        PythonPath = $PythonExe
        Deploy = (Join-Path $ProjectRoot "dist\\engram-overlay")
        NoStart = $true
    }
    & $overlayEngine @engineArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Overlay engine failed; existing artifact was preserved"
    }
}

# 10. Overlay launcher (engram-overlay command)
Write-Step "Overlay launcher..."
$OverlayCmdPath = Join-Path $ShimDir "engram-overlay.cmd"
$overlayCmdLines = @(
    "@echo off",
    "setlocal",
    "set `"DIST_EXE=`"",
    "if not `"%ENGRAM_PROJECT_ROOT%`"==`"`" if exist `"%ENGRAM_PROJECT_ROOT%\dist\engram-overlay\engram-overlay.exe`" set `"DIST_EXE=%ENGRAM_PROJECT_ROOT%\dist\engram-overlay\engram-overlay.exe`"",
    "if `"%DIST_EXE%`"==`"`" if not `"%ENGRAM_WORKDIR%`"==`"`" if exist `"%ENGRAM_WORKDIR%\dist\engram-overlay\engram-overlay.exe`" set `"DIST_EXE=%ENGRAM_WORKDIR%\dist\engram-overlay\engram-overlay.exe`"",
    "if `"%DIST_EXE%`"==`"`" if exist `"$DistExe`" set `"DIST_EXE=$DistExe`"",
    "if `"%DIST_EXE%`"==`"`" (",
    "  echo [engram-overlay] exe not found.",
    "  echo   looked for: %%ENGRAM_PROJECT_ROOT%%\\dist\\engram-overlay\\engram-overlay.exe",
    "  echo   fallback:   %%ENGRAM_WORKDIR%%\\dist\\engram-overlay\\engram-overlay.exe",
    "  echo   installed:  $DistExe",
    "  exit /b 1",
    ")",
    "start `"`" `"%DIST_EXE%`""
)
[System.IO.File]::WriteAllLines($OverlayCmdPath, $overlayCmdLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $OverlayCmdPath

# 11. User config (~/.engram/overlay.user.yaml)
Write-Step "User config..."
$TemplateOverlayConfigPath = Join-Path $ProjectRoot "config\overlay.user.yaml"
if (-not (Test-Path $OverlayUserConfigPath)) {
    if (Test-Path $TemplateOverlayConfigPath) {
        Copy-Item $TemplateOverlayConfigPath $OverlayUserConfigPath
        Write-Ok "Created: $OverlayUserConfigPath"
    } else {
        Write-Warn "Template not found — will be auto-generated on first run"
    }
} else { Write-Ok "Exists: $OverlayUserConfigPath" }

if (-not (Test-Path $OverlayUserConfigPath)) {
    $minimalOverlayUserConfig = @"
cli:
  provider: "$DefaultCliProvider"
"@
    [System.IO.File]::WriteAllText($OverlayUserConfigPath, $minimalOverlayUserConfig, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Created minimal config: $OverlayUserConfigPath"
}

$setOverlayProviderScript = @"
import yaml
path = r'$($OverlayUserConfigPath -replace '\\', '/')'
with open(path, encoding='utf-8') as f:
    data = yaml.safe_load(f) or {}
if not isinstance(data, dict):
    data = {}
cli = data.get('cli')
if not isinstance(cli, dict):
    cli = {}
cli['provider'] = r'$DefaultCliProvider'
if r'$SelectedOllamaModel':
    cli['ollama_model'] = r'$SelectedOllamaModel'
data['cli'] = cli
# MCP HTTP 서버 python_exe 저장 (overlay.exe 동결 모드 대비)
mcp = data.get('mcp')
if not isinstance(mcp, dict):
    mcp = {}
mcp['python_exe'] = r'$($PythonExe -replace '\\', '/')'
mcp['http_port'] = $MCP_HTTP_PORT
data['mcp'] = mcp
with open(path, 'w', encoding='utf-8') as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print('updated')
"@
$setOverlayProviderResult = & $PythonExe -c $setOverlayProviderScript 2>&1
if ($setOverlayProviderResult -like "*updated*") {
    Write-Ok "Set overlay default provider: $DefaultCliProvider"
} else {
    Write-Warn "Could not update overlay default provider: $setOverlayProviderResult"
}

# 11b. .env 템플릿 (~/.engram/.env)
Write-Step ".env 파일..."
$EnvPath = Join-Path $ShimDir ".env"
if (-not (Test-Path $EnvPath)) {
    $envTemplate = @"
# Engram 환경변수 설정
# 이 파일은 ~/.engram/.env 에 위치합니다 (git에 포함되지 않음)

# Discord Bot Token (Discord Developer Portal에서 발급)
# https://discord.com/developers/applications
DISCORD_BOT_TOKEN=
"@
    [System.IO.File]::WriteAllText($EnvPath, $envTemplate, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Created: $EnvPath"
    Write-Warn "DISCORD_BOT_TOKEN을 $EnvPath 에 입력하세요"
} else { Write-Ok "Exists: $EnvPath" }
