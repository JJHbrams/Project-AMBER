# Source installer/development workflow only. Never loaded by the overlay host.
# Startup ownership is separate from runtime ownership: no runtime/config adoption.
. (Join-Path $PSScriptRoot 'external-components.ps1')
function Get-EngramCatalogRuntime {
    param([string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'))
    $generation = Get-EngramComponentGeneration -Root $Root
    $runtime = if ($generation) { $generation.Runtime } else { Join-Path $Root 'runtime' }
    $exe = Join-Path $runtime 'Scripts\engram-custom-overlayw.exe'
    $entry = Join-Path $runtime 'Lib\site-packages\engram_overlay\__main__.py'
    $provider = Join-Path $runtime 'Lib\site-packages\engram_overlay\provider.py'
    $venvConfig = Join-Path $runtime 'pyvenv.cfg'
    $basePython = ''
    if (Test-Path -LiteralPath $venvConfig -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $venvConfig) {
            if ($line -match '^executable\s*=\s*(.+)$') { $basePython = $Matches[1].Trim() }
        }
    }
    if (-not $basePython -or -not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
        throw 'External runtime base Python is missing; repair the installed runtime first.'
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $provider -PathType Leaf) -or
        -not (Test-Path -LiteralPath $entry -PathType Leaf) -or
        -not (Select-String -LiteralPath $entry -SimpleMatch 'run_catalog_provider' -Quiet)) {
        throw 'Installed external catalog provider missing or incompatible. Install/repair engram-overlay first.'
    }
    [pscustomobject]@{ Executable = [IO.Path]::GetFullPath($exe); Arguments = '--provider'; Root = $Root }
}

function Get-EngramExistingStartupHost {
    # dev-rebuild must not silently bind login to a disposable development tree.
    $path = Join-Path ([Environment]::GetFolderPath('Startup')) 'AMBER (ENGRAM).lnk'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'No existing installed host startup. Run INSTALL -AutoStart on from the permanent checkout first.' }
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    if ((Split-Path $link.TargetPath -Leaf) -ne 'engram-overlay.exe' -or $link.Arguments -or
        -not (Test-Path -LiteralPath $link.TargetPath -PathType Leaf)) {
        throw 'Existing startup is not the installed Engram host; preserved. Use INSTALL -AutoStart on.'
    }
    return $link.TargetPath
}

function Get-EngramArtifactOrigin {
    param([string]$Executable)
    if ((Split-Path $Executable -Leaf) -ne 'engram-overlay.exe' -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return '' }
    $manifest = Join-Path (Split-Path $Executable) 'build-manifest.json'
    if (Test-Path -LiteralPath $manifest -PathType Leaf) {
        try {
            $data = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
            if ($data.PSObject.Properties['source_repository'] -and $data.source_repository -match '^[a-f0-9]{64}$') { return [string]$data.source_repository }
        } catch { }
    }
    # Older source INSTALL artifacts have no origin field; inspect their actual
    # checkout's git metadata, never execute commands from it.
    $artifact = Split-Path $Executable
    if ((Split-Path $artifact -Leaf) -ne 'engram-overlay' -or (Split-Path (Split-Path $artifact) -Leaf) -ne 'dist') { return '' }
    $root = Split-Path (Split-Path $artifact)
    $git = Join-Path $root '.git'
    try {
        if (Test-Path -LiteralPath $git -PathType Leaf) {
            $pointer = (Get-Content -LiteralPath $git -Raw).Trim()
            if ($pointer -notmatch '^gitdir:\s*(.+)$') { return '' }
            $git = if ([IO.Path]::IsPathRooted($Matches[1])) { $Matches[1] } else { Join-Path $root $Matches[1] }
            $common = Join-Path $git 'commondir'
            if (Test-Path -LiteralPath $common -PathType Leaf) { $git = [IO.Path]::GetFullPath((Join-Path $git (Get-Content -LiteralPath $common -Raw).Trim())) }
        }
        $text = Get-Content -LiteralPath (Join-Path $git 'config') -Raw
        $origin = [regex]::Match($text, '(?ms)^\[remote "origin"\]\s*(.*?)(?=^\[|\z)')
        $url = [regex]::Match($origin.Groups[1].Value, '(?m)^\s*url\s*=\s*(.+)$').Groups[1].Value.Trim()
        if (-not $url) { return '' }
        if ($url.Contains('://')) { $uri = [Uri]$url; $normalized = $uri.Host + '/' + $uri.AbsolutePath.Trim('/') }
        else { $normalized = ($url.Split('@')[-1] -replace ':', '/').Trim('/') }
        $normalized = ($normalized -replace '\.git$', '').ToLowerInvariant()
        $hash = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)))).Replace('-', '').ToLowerInvariant() }
        finally { $hash.Dispose() }
    } catch { return '' }
}

function Test-EngramHostMigration {
    param([string]$Previous, [string]$Next)
    $previousOrigin = Get-EngramArtifactOrigin -Executable $Previous
    return $previousOrigin -and $previousOrigin -ceq (Get-EngramArtifactOrigin -Executable $Next)
}

function Set-EngramJointStartup {
    param(
        [ValidateSet('preserve','on','off')][string]$Mode = 'preserve',
        [string]$HostExecutable,
        [string]$HostArguments = '',
        [switch]$HostOnly,
        [string]$StartupDirectory = [Environment]::GetFolderPath('Startup'),
        [string]$StateDirectory = (Join-Path $env:USERPROFILE '.engram'),
        [string]$ExternalRoot = (Join-Path $env:LOCALAPPDATA 'engram-overlay'),
        [string]$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    )
    # Called only outside NoStart by the entrypoints. Preserve the user's ON/OFF
    # choice, but finish an exact owned path migration deferred by NoStart.
    if ($Mode -ne 'off') {
        try { Resolve-EngramDeferredComponentStartup -Root $ExternalRoot -StateDirectory $StateDirectory -RunKey $RunKey }
        catch { Write-Warning ('Deferred external login update remains pending; original entry preserved: ' + $_.Exception.Message) }
    }
    if ($Mode -eq 'preserve') { return }
    $recordPath = Join-Path $StateDirectory 'joint-startup.json'
    $linkPath = Join-Path $StartupDirectory 'AMBER (ENGRAM).lnk'
    $record = $null
    if (Test-Path -LiteralPath $recordPath) {
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($record.owner -ne 'engram-joint-startup-v1') { throw 'Unrecognized startup ownership record; preserved.' }
    }
    $shell = New-Object -ComObject WScript.Shell
    $currentLink = if (Test-Path -LiteralPath $linkPath) { $shell.CreateShortcut($linkPath) } else { $null }
    $legacyPath = Join-Path $StartupDirectory 'engram-overlay.lnk'
    $legacy = if (Test-Path -LiteralPath $legacyPath) { $shell.CreateShortcut($legacyPath) } else { $null }
    $legacyOwned = $legacy -and $HostExecutable -and $legacy.TargetPath -eq $HostExecutable -and
        $legacy.Arguments -eq $HostArguments -and ($legacy.Description -like '*ENGRAM*')
    $run = Get-ItemProperty -LiteralPath $RunKey -Name EngramOverlay -ErrorAction SilentlyContinue
    $currentRun = if ($run) { [string]$run.EngramOverlay } else { '' }
    $runtimeStartup = ''
    $runtimeMarker = Join-Path $ExternalRoot 'runtime\.installed-by-amber'
    if (Test-Path -LiteralPath $runtimeMarker -PathType Leaf) {
        try {
            $marker = Get-Content -LiteralPath $runtimeMarker -Raw | ConvertFrom-Json
            if ($marker.PSObject.Properties['autostart_value']) { $runtimeStartup = [string]$marker.autostart_value }
        } catch { }
    }
    if ($Mode -eq 'off') {
        if ($legacyOwned) { Remove-Item -LiteralPath $legacyPath -Force }
        if ($currentRun -and $runtimeStartup -and $currentRun -ceq $runtimeStartup) {
            Remove-ItemProperty -LiteralPath $RunKey -Name EngramOverlay
            $currentRun = ''
        }
        if (-not $record) {
            if ($currentLink -and $HostExecutable -and $currentLink.TargetPath -eq $HostExecutable -and
                $currentLink.Arguments -eq $HostArguments -and $currentLink.Description -like 'AMBER (ENGRAM)*') {
                Remove-Item -LiteralPath $linkPath -Force
            }
            Write-Warning 'Unowned external startup registration preserved.'
            return
        }
        if ($currentLink -and $currentLink.TargetPath -eq $record.host -and $currentLink.Arguments -eq $record.arguments) {
            Remove-Item -LiteralPath $linkPath -Force
        }
        if ($currentRun -and $currentRun -ceq $record.external) {
            Remove-ItemProperty -LiteralPath $RunKey -Name EngramOverlay
        }
        # Keep provenance so a later ON never mistakes a changed entry for ours.
        return
    }
    if (-not (Test-Path -LiteralPath $HostExecutable -PathType Leaf)) { throw 'Host executable missing; startup unchanged.' }
    if ($legacy -and -not $legacyOwned) { throw 'Legacy host startup is user-managed or changed; preserved to avoid duplicate startup.' }
    $desiredRun = ''
    if ($HostOnly -and $record) { $desiredRun = [string]$record.external }
    if (-not $HostOnly) {
        $catalog = Get-EngramCatalogRuntime -Root $ExternalRoot
        $desiredRun = '"{0}" --provider' -f $catalog.Executable
    }
    if (-not $HostOnly -and $currentRun -and $currentRun -cne $desiredRun -and $currentRun -cne $runtimeStartup -and (-not $record -or $currentRun -cne $record.external)) {
        throw 'EngramOverlay Run value is user-managed or changed; startup unchanged.'
    }
    if ($currentLink -and -not (
        ($currentLink.TargetPath -eq $HostExecutable -and $currentLink.Arguments -eq $HostArguments) -or
        (-not $currentLink.Arguments -and -not $HostArguments -and $currentLink.Description -like 'AMBER (ENGRAM)*' -and
         (Test-EngramHostMigration -Previous $currentLink.TargetPath -Next $HostExecutable)) -or
        ($record -and $currentLink.TargetPath -eq $record.host -and $currentLink.Arguments -eq $record.arguments))) {
        throw 'Existing host startup shortcut differs; startup unchanged.'
    }
    New-Item -ItemType Directory -Path $StartupDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
    # Persist exact intended values before writes for recoverable interrupted setup.
    $payload = @{ owner='engram-joint-startup-v1'; host=$HostExecutable; arguments=$HostArguments; external=$desiredRun }
    [IO.File]::WriteAllText($recordPath, ($payload | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $HostExecutable
    $link.Arguments = $HostArguments
    $link.WorkingDirectory = Split-Path $HostExecutable
    $link.Description = 'AMBER (ENGRAM) ' + [char]0x2014 + ' Auto Start'
    $link.WindowStyle = 7
    $link.Save()
    if ($legacyOwned) { Remove-Item -LiteralPath $legacyPath -Force }
    if (-not $HostOnly) {
        New-Item -Path $RunKey -Force | Out-Null
        New-ItemProperty -LiteralPath $RunKey -Name EngramOverlay -Value $desiredRun -PropertyType String -Force | Out-Null
    }
    $verified = $shell.CreateShortcut($linkPath)
    if ($verified.TargetPath -ne $HostExecutable -or $verified.Arguments -ne $HostArguments -or
        (-not $HostOnly -and (Get-ItemPropertyValue -LiteralPath $RunKey -Name EngramOverlay) -cne $desiredRun)) {
        throw 'Joint startup readback verification failed.'
    }
    if ($HostOnly) { Write-Host 'Engram host startup enabled; external startup preserved.' }
    else { Write-Host 'Joint startup enabled: Engram + installed external catalog provider.' }
}

function Start-EngramCatalogRuntime {
    param([string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'),
          [int]$StmPort = 17384, [int]$HostPid = 0,
          [int]$ReadyTimeoutSeconds = 45, [switch]$Required)
    try { $catalog = Get-EngramCatalogRuntime -Root $Root }
    catch { if ($Required) { throw }; Write-Warning $_.Exception.Message; return }
    $scripts = [IO.Path]::GetFullPath((Join-Path $Root 'runtime\Scripts')) + '\'
    $existing = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($scripts, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match '(?:^|\s)--provider(?:\s|$)'
    } | Select-Object -First 1
    if ($existing) { Write-Host "External catalog provider already running: PID=$($existing.ProcessId)" }
    else {
        $process = Start-Process -FilePath $catalog.Executable -ArgumentList '--provider' -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        if ($process.WaitForExit(1000)) { throw "External catalog provider exited (code $($process.ExitCode)); inspect renderer logs." }
    }
    Wait-EngramCatalogReady -StmPort $StmPort -HostPid $HostPid -TimeoutSeconds $ReadyTimeoutSeconds
}

function Wait-EngramCatalogReady {
    param([int]$StmPort, [int]$HostPid, [int]$TimeoutSeconds = 45,
          [string]$DiscoveryPath = (Join-Path $env:USERPROFILE '.engram\overlay-state-api-v1.json'))
    if ($HostPid -le 0) { throw 'Expected host PID is required for external renderer readiness.' }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $discovery = Get-Content -LiteralPath $DiscoveryPath -Raw | ConvertFrom-Json
            if ([int]$discovery.port -ne $StmPort) { throw 'Stale discovery' }
            # The token never enters argv, output, transcript or persisted evidence.
            $headers = @{ Authorization = 'Bearer ' + [string]$discovery.token }
            $state = Invoke-RestMethod -Uri "http://127.0.0.1:$StmPort/state/renderers" -Headers $headers -TimeoutSec 2 -UseBasicParsing
            if ([int]$state.pid -eq $HostPid -and $state.instance_id -eq $discovery.instance_id -and
                $state.catalog_connected -and $state.selected_ready) {
                Write-Host 'External catalog connected to current host; selected renderer readiness verified.'
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    throw 'External renderer connection/selected renderer readiness timed out. Inspect renderer logs; setup is incomplete.'
}

function Get-EngramLaunchContract {
    param([Parameter(Mandatory)][string]$Executable, [string]$Entry = '',
          [ValidateSet('runtime-contract','service-config','claude-monitor-hooks','codex-monitor-hooks')][string]$Role = 'runtime-contract')
    $output = Join-Path ([IO.Path]::GetTempPath()) ('engram-contract-' + [Guid]::NewGuid().ToString('N') + '.json')
    $errorOutput = $output + '.err'
    try {
        $argsText = if ($Entry) { '"{0}" --role {1}' -f $Entry, $Role } else { '--role ' + $Role }
        if ($Role -in @('claude-monitor-hooks','codex-monitor-hooks')) { $argsText += ' --provision --apply' }
        $process = Start-Process -FilePath $Executable -ArgumentList $argsText -WindowStyle Hidden -PassThru -RedirectStandardOutput $output -RedirectStandardError $errorOutput
        $null = $process.Handle
        if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'Host runtime contract timed out.' }
        if ($process.ExitCode -ne 0) { throw 'Host runtime contract failed; inspect runtime logs before launching.' }
        $line = Get-Content -LiteralPath $output | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1
        if (-not $line) { throw 'Host runtime contract returned no JSON evidence.' }
        return $line | ConvertFrom-Json
    } finally {
        foreach ($path in @($output, $errorOutput)) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }
    }
}

function Wait-EngramHostReady {
    param([Parameter(Mandatory)]$Process, [Parameter(Mandatory)]$Contract, [int]$TimeoutSeconds = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) { throw "Host exited before readiness (code $($Process.ExitCode))." }
        try {
            $stm = Invoke-RestMethod -Uri "http://127.0.0.1:$($Contract.stm_port)/health" -TimeoutSec 2 -UseBasicParsing
            $mcp = Invoke-RestMethod -Uri "http://127.0.0.1:$($Contract.mcp_port)/health" -TimeoutSec 2 -UseBasicParsing
            if ($stm.role -eq 'overlay-stm' -and [int]$stm.pid -eq $Process.Id -and
                [int]$mcp.parent_pid -eq $Process.Id -and $mcp.runtime -eq $Contract.runtime) {
                if ($Contract.runtime -eq 'source' -and $mcp.source_root -ne $Contract.source_root) { throw 'Wrong source MCP' }
                $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id)" -ErrorAction Stop)
                if (-not ($children | Where-Object { $_.ProcessId -eq [int]$mcp.pid })) { throw 'MCP is not an OS child of this host' }
                $watcher = $children | Where-Object {
                    if ($Contract.runtime -eq 'source') { $_.CommandLine -and $_.CommandLine.Contains((Join-Path $Contract.source_root 'scripts\kg\kg_watcher.py')) }
                    else { $_.ExecutablePath -eq $Process.Path -and $_.CommandLine -match '--role\s+kg-watcher(?:\s|$)' }
                }
                if (-not $watcher) { throw 'Watcher child not ready' }
                if ($Contract.dashboard_enabled) {
                    $dashboard = Invoke-RestMethod -Uri "http://127.0.0.1:$($Contract.dashboard_port)/_stcore/health" -TimeoutSec 2 -UseBasicParsing
                    if ($dashboard -ne 'ok') { throw 'Dashboard not ready' }
                    $dashboardChild = $children | Where-Object {
                        if ($Contract.runtime -eq 'source') { $_.CommandLine -and $_.CommandLine.Contains((Join-Path $Contract.source_root 'scripts\engram_dashboard.py')) }
                        else { $_.ExecutablePath -eq (Join-Path (Split-Path $Process.Path) 'engram-dashboard.exe') }
                    }
                    if (-not $dashboardChild) { throw 'Dashboard is not owned by this host' }
                }
                Write-Host "Host family ready: PID=$($Process.Id), runtime=$($Contract.runtime)"
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw 'Host service-family readiness timed out; setup is incomplete.'
}

function Invoke-EngramInstalledLaunch {
    param([Parameter(Mandatory)][string]$Executable,
          [ValidateSet('start','skip')][string]$ExternalOverlay = 'start', [switch]$RequireExternal)
    $contract = Get-EngramLaunchContract -Executable $Executable
    if ($ExternalOverlay -eq 'start' -and ($RequireExternal -or [bool]$contract.selected_renderer_id)) { Get-EngramCatalogRuntime | Out-Null }
    $process = Start-Process -FilePath $Executable -WorkingDirectory (Split-Path $Executable) -WindowStyle Hidden -PassThru
    Wait-EngramHostReady -Process $process -Contract $contract
    if ($ExternalOverlay -eq 'start' -and ($RequireExternal -or [bool]$contract.selected_renderer_id)) {
        Start-EngramCatalogRuntime -StmPort $contract.stm_port -HostPid $process.Id -Required:($RequireExternal -or [bool]$contract.selected_renderer_id)
    }
}

function Initialize-EngramExternalRuntime {
    param([ValidateSet('none','reuse','bolttagu-2d','later')][string]$Mode = 'none',
          [string]$HostExecutable, [string]$WheelPath, [string]$PythonCommand = 'python', [switch]$AllowDownloadWheel)
    if ($Mode -eq 'none') { return }
    if ($Mode -eq 'reuse') { Get-EngramCatalogRuntime | Out-Null; return }
    . (Join-Path $PSScriptRoot 'external-overlay.ps1')
    $ownership = Get-ExternalOverlayOwnership
    if ($ownership.Exists -and -not $ownership.OwnedByAmber) {
        Get-EngramCatalogRuntime | Out-Null
        Write-Host 'Reusing validated external runtime; user ownership/configuration preserved.'
    } else {
        if (-not (Test-Path -LiteralPath $WheelPath -PathType Leaf) -and $AllowDownloadWheel) {
            . (Join-Path $PSScriptRoot 'external-wheel.ps1')
            $WheelPath = Resolve-ExternalOverlayWheel -Version (Get-PinnedExternalOverlayVersion)
        }
        if (-not $WheelPath) { throw 'Requested pinned external renderer wheel is unavailable.' }
        $version = (Get-Item -LiteralPath $HostExecutable).VersionInfo.FileVersion
        if (-not $version) { $version = 'source' }
        $result = Install-ExternalOverlayRuntime -WheelPath $WheelPath -AmberVersion $version -Overlay 'bolttagu-2d' -PythonCommand $PythonCommand
        if (-not $result.Installed) { throw ('Requested external runtime could not be installed: ' + $result.Reason) }
        Get-EngramCatalogRuntime | Out-Null
        Write-Host ('External renderer installed: ' + (Get-PinnedExternalOverlayVersion))
    }
    if ($Mode -eq 'bolttagu-2d') {
        $config = Join-Path $env:USERPROFILE '.engram\overlay.user.yaml'
        $arguments = '--role install-user-config --config-path "{0}" --overlay-renderer engram.bolttagu-2d' -f $config
        $process = Start-Process -FilePath $HostExecutable -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
        if ($process.ExitCode -ne 0) { throw 'Could not save the requested validated Bolttagu selection.' }
    }
}
