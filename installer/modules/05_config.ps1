#
# 05_config.ps1 — Runtime config, User config, MCP config (모든 클라이언트)
#   Copilot CLI / Claude Code / Antigravity / VSCode workspace / VSCode global / project .mcp.json
#

# 4b. Runtime config (model/options)
$serviceConfig = & $PythonExe (Join-Path $ProjectRoot 'engram_overlay_entry.py') --role service-config
if ($LASTEXITCODE -ne 0) { throw 'Effective service configuration is invalid; client registration unchanged.' }
$MCP_HTTP_PORT = [int](($serviceConfig | ConvertFrom-Json).mcp_port)
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

# Optional external human-readable daily note folder.
# memory:
#   auto_checkpoint:
#     external_daily_dir: "D:/Notes/daily"

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

# Claude transport is registered below by the preserving bridge adapter.
Write-Step "Claude session lifecycle hooks (compatible CLI only)"
& $PythonExe (Join-Path $ProjectRoot 'engram_overlay_entry.py') --role claude-monitor-hooks --provision --apply
if ($LASTEXITCODE -ne 0) { throw 'Claude lifecycle hook provisioning failed; inspect settings JSON before retrying.' }
# Ensure clean profiles have a discoverable Codex root before the native hook
# provisioner enumerates roots; it still never grants hook trust.
$CodexRoot = Join-Path $env:USERPROFILE '.codex'
if (-not (Test-Path $CodexRoot)) { New-Item -Path $CodexRoot -ItemType Directory -Force | Out-Null }
$CodexConfigPath = Join-Path $CodexRoot 'config.toml'
if (-not (Test-Path $CodexConfigPath)) { [System.IO.File]::WriteAllText($CodexConfigPath, '', [System.Text.UTF8Encoding]::new($false)) }
Write-Step "Codex session lifecycle hooks (/hooks review required; trust is never automatic)"
& $PythonExe (Join-Path $ProjectRoot 'engram_overlay_entry.py') --role codex-monitor-hooks --provision --apply
if ($LASTEXITCODE -ne 0) { throw 'Codex lifecycle hook provisioning failed; inspect settings JSON before retrying.' }

# 5bb. MCP config (Antigravity / AGY) — overlay 수명 공유 HTTP
Write-Step "MCP config (Antigravity)..."
if ($AntigravityCmdDetected) {
    $antigravityMcpOut = & agy mcp add engram "http://127.0.0.1:$MCP_HTTP_PORT/mcp" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Antigravity user MCP server registered: engram (HTTP)"
    } else {
        Write-Warn "Antigravity MCP 등록 실패: $antigravityMcpOut"
        Write-Warn "수동 등록: agy mcp add engram `"http://127.0.0.1:$MCP_HTTP_PORT/mcp`""
    }
} else {
    Write-Warn "Antigravity (agy) not found — skipping MCP setup"
}

# Register only the owned Engram transport. Native hook definitions/trust stay separate.
Write-Step "MCP recovery bridge (Claude and Codex)"
$BridgeLauncher = Join-Path $ProjectRoot 'scripts/engram_mcp_bridge.py'
$BridgeDiscovery = Join-Path $env:USERPROFILE '.engram/overlay-state-api-v1.json'
& $PythonExe $BridgeLauncher --install --auto-home --command $PythonExe `
    "--arg=-u" "--arg=$BridgeLauncher" "--arg=--upstream-url" "--arg=http://127.0.0.1:$MCP_HTTP_PORT/mcp" `
    "--arg=--state-url" "--arg=http://127.0.0.1:17384/state/mcp/events" `
    "--arg=--discovery-file" "--arg=$BridgeDiscovery"
if ($LASTEXITCODE -ne 0) { throw 'MCP bridge registration failed; original configuration was retained for the failing file.' }

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

# Project Claude config can shadow the global entry; merge only Engram.
Write-Step "MCP recovery bridge (project Claude config)"
$ProjectMcpPath = Join-Path $ProjectRoot '.mcp.json'
& $PythonExe $BridgeLauncher --install --claude-config $ProjectMcpPath --command $PythonExe `
    "--arg=-u" "--arg=$BridgeLauncher" "--arg=--upstream-url" "--arg=http://127.0.0.1:$MCP_HTTP_PORT/mcp" `
    "--arg=--state-url" "--arg=http://127.0.0.1:17384/state/mcp/events" `
    "--arg=--discovery-file" "--arg=$BridgeDiscovery"
if ($LASTEXITCODE -ne 0) { throw 'Project MCP bridge registration failed.' }
