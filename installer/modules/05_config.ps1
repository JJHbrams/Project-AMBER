#
# 05_config.ps1 — Runtime config, User config, MCP config (모든 클라이언트)
#   Copilot CLI / Claude Code / Gemini CLI / VSCode workspace / VSCode global / project .mcp.json
#

# 4b. Runtime config (model/options)
Write-Step "Runtime config..."
$CopilotModel = "claude-sonnet-4.6"
$CopilotAllowAllTools = $true
if (Test-Path $RuntimeConfigPath) {
    $runtimeLine = & $PythonExe -c "import yaml; d=yaml.safe_load(open(r'$RuntimeConfigPath',encoding='utf-8')) or {}; c=d.get('copilot') or {}; model=c.get('model','claude-sonnet-4.6'); allow=str(bool(c.get('allow_all_tools',True))).lower(); print(f'{model}|{allow}')" 2>$null
    if ($runtimeLine -and ($runtimeLine -like "*|*")) {
        $parts = $runtimeLine.Trim().Split("|", 2)
        if ($parts[0]) { $CopilotModel = $parts[0] }
        if ($parts[1]) { $CopilotAllowAllTools = ($parts[1].ToLower() -eq "true") }
    }
}
$CopilotAllowArg = if ($CopilotAllowAllTools) { " --allow-all-tools" } else { "" }
$EngramCopilotCmd = "copilot --model $CopilotModel --additional-mcp-config @`"%MCP_CONFIG%`"$CopilotAllowArg"
Write-Ok "model=$CopilotModel, allow_all_tools=$CopilotAllowAllTools"

# 4c. User config (~/.engram/user.config.yaml)
Write-Step "User config..."
if (-not (Test-Path $ShimDir)) { New-Item -Path $ShimDir -ItemType Directory -Force | Out-Null }
if (-not (Test-Path $UserConfigPath)) {
    $userConfig = @"
# User runtime overrides for Engram.
db:
  root_dir: "$DbDir"

workdir: "$WorkDir"

# watch_workspaces: git 프로젝트들이 모여있는 상위 디렉토리 목록.
# 하위 git repo를 자동 탐색하여 개념 파일(README, architecture 등) 변경 시
# wiki의 docs/projects/<repo-name>/ 에 자동으로 반영됩니다.
#
# watch_workspaces:
#   - C:/Users/yourname/Desktop/Workspace
#
# watch_conceptual_files:  # 기본값 (변경 시 아래처럼 재정의)
#   - README.md
#   - architecture.md
#   - docs/architecture.md
"@
    [System.IO.File]::WriteAllText($UserConfigPath, $userConfig, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Created: $UserConfigPath"
} else {
    # 기존 파일: db.root_dir, workdir 업데이트 (나머지 설정 보존)
    $updateScript = @"
import yaml
path = r'$($UserConfigPath -replace '\\', '/')'
with open(path, encoding='utf-8') as f:
    d = yaml.safe_load(f) or {}
d.setdefault('db', {})['root_dir'] = r'$($DbDir -replace '\\', '/')'
d['workdir'] = r'$($WorkDir -replace '\\', '/')'
with open(path, 'w', encoding='utf-8') as f:
    yaml.dump(d, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print('updated')
"@
    $updateResult = & $PythonExe -c $updateScript 2>&1
    if ($updateResult -like "*updated*") {
        Write-Ok "Updated: $UserConfigPath  (db.root_dir, workdir)"
    } else {
        Write-Warn "Could not auto-update user config: $updateResult"
        Write-Ok "Exists: $UserConfigPath"
    }
}

# 5. MCP config (Copilot CLI) — overlay 수명 공유 HTTP (기존 항목 보존, merge)
Write-Step "MCP config (Copilot CLI)..."
$mcpDir = Split-Path $McpConfigPath
if (-not (Test-Path $mcpDir)) { New-Item -Path $mcpDir -ItemType Directory -Force | Out-Null }
$engramMcpEntry = [PSCustomObject]@{ type = "http"; url = "http://127.0.0.1:$MCP_HTTP_PORT/mcp" }
if (Test-Path $McpConfigPath) {
    try {
        $existingMcp = Get-Content $McpConfigPath -Raw | ConvertFrom-Json
        if (-not $existingMcp.mcpServers) {
            $existingMcp | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([PSCustomObject]@{}) -Force
        }
        # 구 이름(continuum) 정리
        if ($existingMcp.mcpServers.PSObject.Properties["continuum"]) {
            $existingMcp.mcpServers.PSObject.Properties.Remove("continuum")
            Write-Ok "Removed legacy 'continuum' MCP entry"
        }
        if ($existingMcp.mcpServers.PSObject.Properties["engram"]) {
            $existingMcp.mcpServers.PSObject.Properties.Remove("engram")
        }
        $existingMcp.mcpServers | Add-Member -NotePropertyName engram -NotePropertyValue $engramMcpEntry
        $mcpJson = $existingMcp | ConvertTo-Json -Depth 6
    } catch {
        # 파싱 실패 시 새로 작성
        $mcpJson = @{ mcpServers = @{ engram = $engramMcpEntry } } | ConvertTo-Json -Depth 5
    }
} else {
    $mcpJson = @{ mcpServers = @{ engram = $engramMcpEntry } } | ConvertTo-Json -Depth 5
}
[System.IO.File]::WriteAllText($McpConfigPath, $mcpJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $McpConfigPath

# 5b. MCP config (Claude Code)
Write-Step "MCP config (Claude Code)..."
$httpEntry = [PSCustomObject]@{ type = "http"; url = "http://127.0.0.1:$MCP_HTTP_PORT/mcp" }
if (Test-Path $ClaudeConfigPath) {
    $claudeConfig = Get-Content $ClaudeConfigPath -Raw | ConvertFrom-Json
    if (-not $claudeConfig.mcpServers) {
        $claudeConfig | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    # 구 이름(stdio) 정리
    foreach ($serverProp in @($claudeConfig.mcpServers.PSObject.Properties)) {
        $serverArgs = @($serverProp.Value.args)
        if ($serverProp.Name -ne "engram" -and $serverArgs -contains $McpServerScript) {
            $claudeConfig.mcpServers.PSObject.Properties.Remove($serverProp.Name)
        }
    }
    $claudeConfig.mcpServers | Add-Member -NotePropertyName engram -NotePropertyValue $httpEntry -Force
    $claudeJson = $claudeConfig | ConvertTo-Json -Depth 10
} else {
    $claudeJson = @{ mcpServers = @{ engram = $httpEntry } } | ConvertTo-Json -Depth 5
}
[System.IO.File]::WriteAllText($ClaudeConfigPath, $claudeJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $ClaudeConfigPath

$claudeMcpJson = @{ mcpServers = @{ engram = @{ type = "http"; url = "http://127.0.0.1:$MCP_HTTP_PORT/mcp" } } } | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($ClaudeMcpConfigPath, $claudeMcpJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $ClaudeMcpConfigPath

$claudeProjectHardeningScript = @"
import json
from pathlib import Path

config_path = Path(r'$($ClaudeConfigPath -replace '\\', '/')')
work_dir = r'$WorkDir'
mcp_url = 'http://127.0.0.1:$MCP_HTTP_PORT/mcp'

data = {}
if config_path.exists():
    try:
        loaded = json.loads(config_path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}

mcp_servers = data.get('mcpServers')
if not isinstance(mcp_servers, dict):
    mcp_servers = {}
mcp_servers['engram'] = {'type': 'http', 'url': mcp_url}
data['mcpServers'] = mcp_servers

projects = data.get('projects')
if not isinstance(projects, dict):
    projects = {}
project_cfg = projects.get(work_dir)
if not isinstance(project_cfg, dict):
    project_cfg = {}

enabled = project_cfg.get('enabledMcpjsonServers')
if not isinstance(enabled, list):
    enabled = []
enabled = [name for name in enabled if isinstance(name, str)]
if 'engram' not in enabled:
    enabled.append('engram')
project_cfg['enabledMcpjsonServers'] = enabled

disabled = project_cfg.get('disabledMcpjsonServers')
if not isinstance(disabled, list):
    disabled = []
disabled = [name for name in disabled if isinstance(name, str) and name != 'engram']
project_cfg['disabledMcpjsonServers'] = disabled

project_mcp = project_cfg.get('mcpServers')
if not isinstance(project_mcp, dict):
    project_mcp = {}
project_mcp['engram'] = {'type': 'http', 'url': mcp_url}
project_cfg['mcpServers'] = project_mcp

projects[work_dir] = project_cfg
data['projects'] = projects

config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
print('ok')
"@
$claudeHardeningResult = Invoke-PythonScriptText -PythonPath $PythonExe -ScriptText $claudeProjectHardeningScript
if ($claudeHardeningResult -like "*ok*") {
    Write-Ok "Claude project MCP hardening applied"
} else {
    Write-Warn "Claude project MCP hardening failed: $claudeHardeningResult"
}

# 5bb. MCP config (Gemini CLI) — overlay 수명 공유 HTTP
Write-Step "MCP config (Gemini CLI)..."
$geminiCmd = $GeminiCmdDetected
if ($geminiCmd) {
    & gemini mcp remove --scope user engram *> $null
    $geminiMcpOut = & gemini mcp add --scope user --transport http engram "http://127.0.0.1:$MCP_HTTP_PORT/mcp" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Gemini user MCP server registered: engram (HTTP)"
    } else {
        Write-Warn "Gemini MCP HTTP 등록 실패 — stdio 폴백 시도"
        & gemini mcp remove --scope user engram *> $null
        $geminiMcpOut2 = & gemini mcp add --scope user --transport stdio -e "ENGRAM_DB_DIR=$DbDir" engram $PythonExe $McpServerScript 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Gemini MCP (stdio 폴백) 등록됨"
        } else {
            Write-Warn "Gemini MCP 등록 실패: $geminiMcpOut2"
            Write-Warn "수동 등록: gemini mcp add --scope user --transport http engram `"http://127.0.0.1:$MCP_HTTP_PORT/mcp`""
        }
    }
} else {
    Write-Warn "Gemini CLI not found — skipping Gemini MCP setup"
}

# 5c. MCP config (VSCode Copilot Chat — workspace)
Write-Step "MCP config (VSCode Copilot Chat)..."
$VscodeMcpDir = Join-Path $ProjectRoot ".vscode"
$VscodeMcpPath = Join-Path $VscodeMcpDir "mcp.json"
if (-not (Test-Path $VscodeMcpDir)) { New-Item -Path $VscodeMcpDir -ItemType Directory -Force | Out-Null }
$vscodeMcpJson = @{ servers = @{ engram = @{ type = "http"; url = "http://127.0.0.1:$MCP_HTTP_PORT/mcp" } } } | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($VscodeMcpPath, $vscodeMcpJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $VscodeMcpPath

# 5d. MCP config (VSCode Copilot Chat — global, 다른 프로젝트에서도 engram 사용)
Write-Step "MCP config (VSCode Copilot Chat global)..."
$VscodeGlobalMcpPath = Join-Path $env:APPDATA "Code\User\mcp.json"
$engramServer = @{ type = "http"; url = "http://127.0.0.1:$MCP_HTTP_PORT/mcp" }
if (Test-Path $VscodeGlobalMcpPath) {
    try {
        $globalMcp = Get-Content $VscodeGlobalMcpPath -Raw | ConvertFrom-Json
        if (-not $globalMcp.servers) { $globalMcp | Add-Member -NotePropertyName servers -NotePropertyValue ([PSCustomObject]@{}) }
        # 구 이름 continuum 제거
        if ($globalMcp.servers.PSObject.Properties["continuum"]) {
            $globalMcp.servers.PSObject.Properties.Remove("continuum")
            Write-Ok "Removed legacy 'continuum' server entry"
        }
        # engram 항목 추가/갱신
        if ($globalMcp.servers.PSObject.Properties["engram"]) {
            $globalMcp.servers.PSObject.Properties.Remove("engram")
        }
        $globalMcp.servers | Add-Member -NotePropertyName engram -NotePropertyValue $engramServer
        $globalMcpJson = $globalMcp | ConvertTo-Json -Depth 6
    } catch {
        # 파싱 실패 시 새로 작성
        $globalMcpJson = @{ servers = @{ engram = $engramServer }; inputs = @() } | ConvertTo-Json -Depth 5
    }
} else {
    $globalMcpJson = @{ servers = @{ engram = $engramServer }; inputs = @() } | ConvertTo-Json -Depth 5
}
[System.IO.File]::WriteAllText($VscodeGlobalMcpPath, $globalMcpJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $VscodeGlobalMcpPath

# 5e. MCP config (project-local .mcp.json for Claude Code and compatible clients)
Write-Step "MCP config (project .mcp.json)..."
$ProjectMcpPath = Join-Path $ProjectRoot ".mcp.json"
$projectMcpJson = @{
    mcpServers = @{
        engram = @{
            type = "http"
            url  = "http://127.0.0.1:$MCP_HTTP_PORT/mcp"
        }
    }
} | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($ProjectMcpPath, $projectMcpJson, [System.Text.UTF8Encoding]::new($false))
Write-Ok $ProjectMcpPath
