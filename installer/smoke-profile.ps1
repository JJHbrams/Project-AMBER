# Build validation must never inherit the interactive user's DB or discovery files.
function Enter-EngramBuildSmokeProfile {
    if (-not (Get-Variable EngramBuildSmokeProfile -Scope Script -ErrorAction SilentlyContinue)) {
        $script:EngramBuildSmokeProfile = Join-Path ([IO.Path]::GetTempPath()) ("engram-build-smoke-" + [Guid]::NewGuid().ToString('N'))
        foreach ($relative in @('', '.engram', 'AppData\Roaming', 'AppData\Local', 'db', 'codex', 'claude')) {
            New-Item -ItemType Directory -Path (Join-Path $script:EngramBuildSmokeProfile $relative) -Force | Out-Null
        }
        $ports = @()
        $reservations = @()
        try {
            1..3 | ForEach-Object {
                $socket = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
                $socket.Start()
                $reservations += $socket
                $ports += ([Net.IPEndPoint]$socket.LocalEndpoint).Port
            }
        } finally { foreach ($socket in $reservations) { $socket.Stop() } }
        $settings = @{
            db = @{ root_dir = (Join-Path $script:EngramBuildSmokeProfile 'db') }
            overlay = @{ stm_server_port = $ports[0] }
            mcp = @{ http_port = $ports[1] }
            dashboard = @{ enabled = $false; port = $ports[2] }
        } | ConvertTo-Json -Depth 5
        foreach ($name in @('user.config.yaml', 'overlay.user.yaml')) {
            [IO.File]::WriteAllText((Join-Path $script:EngramBuildSmokeProfile ".engram\$name"), $settings, [Text.UTF8Encoding]::new($false))
        }
        Write-Host "Build smoke profile: $script:EngramBuildSmokeProfile (retained for diagnostics)"
    }
    $values = @{
        HOME = $script:EngramBuildSmokeProfile
        USERPROFILE = $script:EngramBuildSmokeProfile
        APPDATA = (Join-Path $script:EngramBuildSmokeProfile 'AppData\Roaming')
        LOCALAPPDATA = (Join-Path $script:EngramBuildSmokeProfile 'AppData\Local')
        CODEX_HOME = (Join-Path $script:EngramBuildSmokeProfile 'codex')
        CLAUDE_CONFIG_DIR = (Join-Path $script:EngramBuildSmokeProfile 'claude')
        ENGRAM_SMOKE_DB_DIR = (Join-Path $script:EngramBuildSmokeProfile 'db')
        ENGRAM_BUILD_SMOKE = '1'
        ENGRAM_RUNTIME_ROLE = $null
        ENGRAM_STM_PORT = $null
    }
    $saved = @{}
    foreach ($name in $values.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
    }
    return $saved
}

function Exit-EngramBuildSmokeProfile([hashtable]$Saved) {
    foreach ($name in $Saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $Saved[$name], 'Process')
    }
}
