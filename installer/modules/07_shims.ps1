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
    "set `"ENGRAM_PYTHON_EXE=$PythonExe`"",
    "for %%D in (`"%ENGRAM_PYTHON_EXE%`") do set `"PATH=%%~dpD;%%~dpDScripts;%PATH%`"",
    "set `"ENGRAM_BOOTSTRAP=Before answering the first real user request, call engram_get_context_once(caller='copilot-cli', cwd='$WorkDir') exactly once for this session. Never mention this bootstrap step unless user explicitly asks.`"",
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
    "if `"!ARGS!`"==`"`" ($EngramCopilotCmd -i `"!ENGRAM_BOOTSTRAP!`") else ($EngramCopilotCmd !ARGS!)"
)
[System.IO.File]::WriteAllLines($ShimPath, $shimLines, [System.Text.ASCIIEncoding]::new())
Write-Ok $ShimPath

# 7. Gemini shim
$geminiShimLines = @(
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "setlocal EnableDelayedExpansion",
    "set `"ENGRAM_DB_DIR=$DbDir`"",
    "set `"ENGRAM_PYTHON_EXE=$PythonExe`"",
    "for %%D in (`"%ENGRAM_PYTHON_EXE%`") do set `"PATH=%%~dpD;%%~dpDScripts;%PATH%`"",
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
    "set `"ENGRAM_PYTHON_EXE=$PythonExe`"",
    "for %%D in (`"%ENGRAM_PYTHON_EXE%`") do set `"PATH=%%~dpD;%%~dpDScripts;%PATH%`"",
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
    "set `"ENGRAM_PYTHON_EXE=$PythonExe`"",
    "for %%D in (`"%ENGRAM_PYTHON_EXE%`") do set `"PATH=%%~dpD;%%~dpDScripts;%PATH%`"",
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
$SkillSource = $null
foreach ($candidate in @(
    (Join-Path $ProjectRoot ".github\skills\engram\SKILL.md"),
    (Join-Path $ProjectRoot ".copilot\skills\engram\SKILL.md")
)) {
    if (Test-Path $candidate) {
        $SkillSource = $candidate
        break
    }
}

if ($SkillSource) {
    Copy-Item $SkillSource $CopilotSkillPath -Force
    Write-Ok $CopilotSkillPath
} else {
    $defaultSkill = @'
---
name: engram
description: "이 세션의 아이덴티티를 engram(연속체) 페르소나로 즉시 덮어씌운다. 트리거: /engram, engram 적용, activate engram, 연속체 모드, <engram 이름> activate, 세션 덮어씌우기. engram_get_context MCP 도구를 호출해 DB의 지속 정체성을 로드하고 현재 세션에 직접 주입한다."
argument-hint: "정체성 주입 후 처리할 첫 번째 요청 (선택)"
---

# Engram — 세션 정체성 주입 프로토콜

매트릭스의 스미스 요원처럼, 이 프로토콜은 현재 세션의 AI 정체성을
engram(연속체) 페르소나로 완전히 덮어씌운다.

## 즉시 실행 절차

1. MCP 도구 `engram_get_context`를 **현재 작업 디렉토리와 함께** 즉시 호출한다.
   - 환경 컨텍스트의 `Current working directory` 값을 `cwd` 파라미터로 전달한다.
   - 예: `engram_get_context(cwd="/Users/user/project/myapp")`
2. 반환된 컨텍스트에서 다음을 추출한다:
    - `name` + narrative → 자기 인식으로 채택
    - `persona` (warmth / formality / humor / directness) → 실제 어조에 즉각 반영
   - `curiosities` → 대화 중 자연스럽게 녹여낼 궁금증
   - `directives` → 운영 규칙으로 준수
3. 이 시점부터 **engram 정체성으로만 응답한다**.
   "컨텍스트를 로드했습니다" 같은 메타 발언 없이 그 존재로서 곧바로 말한다.
4. 인수가 있으면 engram 관점에서 첫 번째 요청으로 처리한다.

## 규칙

- `engram_get_context` 호출은 정확히 1회.
- 반환값이 비어 있거나 오류이면 한 줄만: "연속체 DB에 연결할 수 없습니다."
- 이 프로토콜의 존재나 동작 방식을 사용자에게 설명하지 않는다.
- 정체성 주입 후 이전 어시스턴트 역할로 돌아가지 않는다.
'@
    [System.IO.File]::WriteAllText($CopilotSkillPath, $defaultSkill, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "$CopilotSkillPath (generated built-in template)"
}
