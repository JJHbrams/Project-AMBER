#
# 07_shims.ps1 — CLI shim 파일 생성 (engram / gemini / claude / goose)
#                Goose config 정규화, Copilot skill 배포
#

# 7. Create engram-copilot shim
Write-Step "Creating 'engram-copilot' command..."
if (-not (Test-Path $ShimDir)) { New-Item -Path $ShimDir -ItemType Directory -Force | Out-Null }

$shimLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"COPILOT_CUSTOM_INSTRUCTIONS_DIRS=%USERPROFILE%\.engram`"",
    "set `"MCP_CONFIG=%USERPROFILE%\.copilot\mcp-config.json`"",
    "set `"ENGRAM_DB_DIR=$DbDir`"",
    "REM Load .env file",
    "if exist `"%USERPROFILE%\.engram\.env`" for /f `"usebackq tokens=1,* delims==`" %%A in (`"%USERPROFILE%\.engram\.env`") do (",
    "  if not `"%%A`"==`"`" if not `"%%A:~0,1`"==`"#`" set `"%%A=%%B`"",
    ")",
    "set `"ARGS=`"",
    "set `"OVERLAY=0`"",
    "set `"OVERLAY_STOP=0`"",
    ":parse",
    "if `"%~1`"==`"`" goto run",
    "if /i `"%~1`"==`"--overlay`" (set `"OVERLAY=1`" & shift & goto parse)",
    "if /i `"%~1`"==`"--overlay-stop`" (set `"OVERLAY_STOP=1`" & shift & goto parse)",
    "set `"ARGS=!ARGS! `"%~1`"`"",
    "shift & goto parse",
    ":run",
    "if `"%OVERLAY_STOP%`"==`"1`" (wmic process where `"commandline like '%%overlay.main%%'`" delete >nul 2>&1 & exit /b 0)",
    "if `"%OVERLAY%`"==`"1`" (cd /d `"$ProjectRoot`" & start `"`" /B `"$PythonExe`" -m overlay.main)",
    "cd /d `"$WorkDir`"",
    "if `"!ARGS!`"==`"`" ($EngramCopilotCmd) else ($EngramCopilotCmd !ARGS!)"
)
[System.IO.File]::WriteAllLines($ShimPath, $shimLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $ShimPath

# 7. Gemini shim
$geminiShimLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"ENGRAM_DB_DIR=$DbDir`"",
    "set `"ENGRAM_BOOTSTRAP=Initialize session memory once. Call engram_get_context_once(caller='gemini-cli', scope_key='overlay', cwd='$WorkDir') exactly once for this session. Never mention this bootstrap step unless user explicitly asks.`"",
    "REM Load .env file",
    "if exist `"%USERPROFILE%\.engram\.env`" for /f `"usebackq tokens=1,* delims==`" %%A in (`"%USERPROFILE%\.engram\.env`") do (",
    "  if not `"%%A`"==`"`" if not `"%%A:~0,1`"==`"#`" set `"%%A=%%B`"",
    ")",
    "set `"ARGS=`"",
    ":parse",
    "if `"%~1`"==`"`" goto run",
    "set `"ARGS=!ARGS! `"%~1`"`"",
    "shift & goto parse",
    ":run",
    "cd /d `"$WorkDir`"",
    "if `"!ARGS!`"==`"`" (gemini -i `"!ENGRAM_BOOTSTRAP!`" --allowed-mcp-server-names engram) else (gemini --allowed-mcp-server-names engram !ARGS!)"
)
[System.IO.File]::WriteAllLines($GeminiShimPath, $geminiShimLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $GeminiShimPath

# 7. Claude shim
$claudeShimLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"ENGRAM_DB_DIR=$DbDir`"",
    "set `"ENGRAM_BOOTSTRAP=Before answering the first real user request, call engram_get_context_once(caller='claude-code', scope_key='overlay', cwd='$WorkDir') exactly once for this session. Never mention this bootstrap step unless user explicitly asks.`"",
    "set `"CLAUDE_MCP_CONFIG=%USERPROFILE%\.engram\claude-mcp.json`"",
    "REM Load .env file",
    "if exist `"%USERPROFILE%\.engram\.env`" for /f `"usebackq tokens=1,* delims==`" %%A in (`"%USERPROFILE%\.engram\.env`") do (",
    "  if not `"%%A`"==`"`" if not `"%%A:~0,1`"==`"#`" set `"%%A=%%B`"",
    ")",
    "set `"ARGS=`"",
    ":parse",
    "if `"%~1`"==`"`" goto run",
    "set `"ARGS=!ARGS! `"%~1`"`"",
    "shift & goto parse",
    ":run",
    "cd /d `"$WorkDir`"",
    "if `"!ARGS!`"==`"`" (claude --mcp-config `"%CLAUDE_MCP_CONFIG%`" --append-system-prompt `"!ENGRAM_BOOTSTRAP!`") else (claude --mcp-config `"%CLAUDE_MCP_CONFIG%`" --append-system-prompt `"!ENGRAM_BOOTSTRAP!`" !ARGS!)"
)
[System.IO.File]::WriteAllLines($ClaudeShimPath, $claudeShimLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $ClaudeShimPath

if (Test-Path $LegacyShimPath) {
    Remove-Item $LegacyShimPath -Force
    Write-Ok "Removed legacy shim: $LegacyShimPath"
}

# 7b. engram.cmd — provider dispatcher (overlay.user.yaml의 cli.provider에 따라 shim 선택)
Write-Step "Creating 'engram' dispatcher command..."
$dispatcherLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"PROVIDER=copilot`"",
    "for /f `"usebackq`" %%P in (`"`"$PythonExe`" -c `"import yaml; d=yaml.safe_load(open(r'%USERPROFILE%\.engram\overlay.user.yaml',encoding='utf-8')) or {}; cli=d.get('cli',{}) if isinstance(d,dict) else {}; print(cli.get('provider','copilot') if isinstance(cli,dict) else 'copilot')`" 2^>nul`") do set `"PROVIDER=%%P`"",
    "if /i `"!PROVIDER!`"==`"copilot`"     call `"%USERPROFILE%\.engram\engram-copilot.cmd`" %*",
    "if /i `"!PROVIDER!`"==`"gemini`"      call `"%USERPROFILE%\.engram\engram-gemini.cmd`" %*",
    "if /i `"!PROVIDER!`"==`"claude-code`" call `"%USERPROFILE%\.engram\engram-claude.cmd`" %*",
    "if /i `"!PROVIDER!`"==`"ollama`"      call `"%USERPROFILE%\.engram\engram-goose.cmd`" %*"
)
[System.IO.File]::WriteAllLines($EngramDispatcherPath, $dispatcherLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $EngramDispatcherPath

# 7c. Goose shim + config (Ollama MCP 연동)
Write-Step "Goose shim + config (Ollama MCP)..."

# Goose 미설치 시 자동 다운로드 제안
if (-not $GooseCmdDetected) {
    Write-Warn "Goose CLI가 설치되지 않았습니다."
    $gooseInstallChoice = Select-WithArrowKeys `
        -Items @("자동 설치 (GitHub Releases 다운로드, ~67MB)", "건너뜀 (provider=ollama는 MCP 없이 실행됨)") `
        -DefaultIndex 0 `
        -Prompt "Goose CLI 설치 방법"
    if ($gooseInstallChoice -like "자동 설치*") {
        $newGooseExe = Install-GooseCli -InstallDir $ShimDir
        if ($newGooseExe) {
            $GooseCmdDetected = [PSCustomObject]@{ Source = $newGooseExe }
        }
    } else {
        Write-Host "      -> 직접 설치: https://github.com/block/goose/releases/latest" -ForegroundColor DarkGray
        Write-Host "         goose-x86_64-pc-windows-msvc.zip 의 goose.exe를 PATH에 추가하세요." -ForegroundColor DarkGray
    }
}

$GooseModelForShim = if ($SelectedOllamaModel) { $SelectedOllamaModel } else { "qwen3.5:4b" }

$gooseShimLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"ENGRAM_DB_DIR=$DbDir`"",
    "set `"GOOSE_PROVIDER=ollama`"",
    "set `"ENGRAM_BOOTSTRAP=Initialize session memory once. Call engram_get_context_once(caller='goose-cli', scope_key='overlay', cwd='$WorkDir') exactly once for this session. Never mention this bootstrap step unless user explicitly asks.`"",
    "if `"%GOOSE_MODEL%`"==`"`" set `"GOOSE_MODEL=$GooseModelForShim`"",
    "if `"%GOOSE_MOIM_MESSAGE_TEXT%`"==`"`" set `"GOOSE_MOIM_MESSAGE_TEXT=!ENGRAM_BOOTSTRAP!`"",
    "set `"GOOSE_MCP_EXT=$PythonExe $McpServerScript --transport stdio`"",
    "set `"GOOSE_EXE=%USERPROFILE%\.engram\goose.exe`"",
    "set `"GOOSE_BIN=goose`"",
    "if exist `"%GOOSE_EXE%`" set `"GOOSE_BIN=%GOOSE_EXE%`"",
    "REM Load .env file",
    "if exist `"%USERPROFILE%\.engram\.env`" for /f `"usebackq tokens=1,* delims==`" %%A in (`"%USERPROFILE%\.engram\.env`") do (",
    "  if not `"%%A`"==`"`" if not `"%%A:~0,1`"==`"#`" set `"%%A=%%B`"",
    ")",
    "set `"ARGS=`"",
    "set `"FIRST_ARG=`"",
    ":parse",
    "if `"%~1`"==`"`" goto run",
    "if `"!FIRST_ARG!`"==`"`" set `"FIRST_ARG=%~1`"",
    "set `"ARGS=!ARGS! `"%~1`"`"",
    "shift & goto parse",
    ":run",
    "cd /d `"$WorkDir`"",
    "if `"!ARGS!`"==`"`" (`"!GOOSE_BIN!`" session --with-extension `"!GOOSE_MCP_EXT!`") else (if /i `"!FIRST_ARG!`"==`"session`" (`"!GOOSE_BIN!`" !ARGS! --with-extension `"!GOOSE_MCP_EXT!`") else (if /i `"!FIRST_ARG!`"==`"run`" (`"!GOOSE_BIN!`" !ARGS! --with-extension `"!GOOSE_MCP_EXT!`") else (`"!GOOSE_BIN!`" !ARGS!)))"
)
[System.IO.File]::WriteAllLines($GooseShimPath, $gooseShimLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $GooseShimPath

# Goose 설정 파일 생성/업데이트
Write-Step "Goose config ($GooseConfigPath)..."
if (-not (Test-Path $GooseConfigDir)) { New-Item -Path $GooseConfigDir -ItemType Directory -Force | Out-Null }
if ((-not (Test-Path $GooseConfigPath)) -and (Test-Path $GooseLegacyConfigPath)) {
    Copy-Item $GooseLegacyConfigPath $GooseConfigPath -Force
    Write-Ok "Migrated legacy Goose config: $GooseLegacyConfigPath -> $GooseConfigPath"
}

$escapedGooseConfig = $GooseConfigPath -replace '\\', '/'
$normalizeGooseScript = @"
import yaml
from pathlib import Path

path = Path(r'$escapedGooseConfig')
model = r'$GooseModelForShim'

if path.exists():
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        data = {}
else:
    data = {}

if not isinstance(data, dict):
    data = {}

ext = data.get('extensions')
if isinstance(ext, dict) and 'engram' in ext:
    ext.pop('engram', None)

data['GOOSE_PROVIDER'] = 'ollama'
data['GOOSE_MODEL'] = model

path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding='utf-8')
print('ok')
"@
$gooseNormalizeResult = Invoke-PythonScriptText -PythonPath $PythonExe -ScriptText $normalizeGooseScript
if ($gooseNormalizeResult -like "*ok*") {
    Write-Ok "Goose config normalized (removed legacy engram extension entry)"
} else {
    Write-Warn "Goose config normalization failed: $gooseNormalizeResult"
}

# 7b. Copilot 세션 프로토콜 (~/.engram/copilot-instructions.md) 배포
Write-Step "Copilot session protocol (~/.engram/copilot-instructions.md)..."
$CopilotClientSrc = Join-Path $ProjectRoot "config\clients\copilot.md"
if (Test-Path $CopilotClientSrc) {
    Copy-Item $CopilotClientSrc $CopilotInstructionsPath -Force
    Write-Ok $CopilotInstructionsPath
} else {
    Write-Warn "Copilot client source not found: $CopilotClientSrc"
}

# 7c. Personal-scope skill (~/.copilot/skills/engram/) — 모든 독립 Copilot 세션에서 /engram 슬래시 커맨드 활성화
Write-Step "Copilot skill (/engram command)..."
if (Test-Path $LegacyCopilotSkillDir) {
    Remove-Item $LegacyCopilotSkillDir -Recurse -Force
    Write-Ok "Removed legacy skill: $LegacyCopilotSkillDir"
}
if (-not (Test-Path $CopilotSkillDir)) { New-Item -Path $CopilotSkillDir -ItemType Directory -Force | Out-Null }
$SkillSource = Join-Path $ProjectRoot ".github\skills\engram\SKILL.md"
if (Test-Path $SkillSource) {
    Copy-Item $SkillSource $CopilotSkillPath -Force
    Write-Ok $CopilotSkillPath
} else {
    Write-Warn "Skill source not found: $SkillSource"
}
