# Shared source/setup component installation. Loading this file is read-only.
# Venvs are created at their permanent path: pip launchers must never be relocated.
function Get-EngramComponentHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Assert-EngramComponentPath {
    param([string]$Root, [string]$Path)
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -ne $base -and -not $full.StartsWith($base + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Component path escapes the managed root.'
    }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Component paths must not traverse reparse points.'
            }
        }
        $parent = Split-Path $cursor -Parent
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}

function Read-EngramComponentJson {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or (Get-Item -LiteralPath $Path).Length -gt 2097152) {
        throw 'Component metadata missing or too large.'
    }
    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop)
}

function Assert-EngramComponentPin {
    param([string]$ManifestPath)
    $pin = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'external-components.pin') -Raw).Trim()
    if ($pin -notmatch '^[a-f0-9]{64}$' -or (Get-EngramComponentHash $ManifestPath) -cne $pin) {
        throw 'External component bundle does not match the installer pin.'
    }
}

function Get-EngramComponentGeneration {
    param([string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'))
    $pointerPath = Assert-EngramComponentPath $Root (Join-Path $Root 'active-generation.json')
    if (-not (Test-Path -LiteralPath $pointerPath)) { return $null }
    $pointer = Read-EngramComponentJson $pointerPath
    if (@(Compare-Object @($pointer.PSObject.Properties.Name) @('schema','owner','generation','marker_sha256')).Count -or
        $pointer.schema -ne 1 -or $pointer.owner -ne 'engram' -or
        $pointer.generation -notmatch '^generations/[a-f0-9]{32}$' -or
        $pointer.marker_sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid managed generation pointer; preserved.' }
    $generation = Assert-EngramComponentPath $Root (Join-Path $Root $pointer.generation)
    $markerPath = Assert-EngramComponentPath $Root (Join-Path $generation 'engram-overlay-components.json')
    if ((Get-EngramComponentHash $markerPath) -ne $pointer.marker_sha256) {
        throw 'Managed generation marker digest mismatch; preserved.'
    }
    $marker = Read-EngramComponentJson $markerPath
    if (@(Compare-Object @($marker.PSObject.Properties.Name) @('schema','package_version','registry_revision','ownership','manifest_sha256','installed_components','installed_payloads')).Count -or
        $marker.schema -ne 1 -or $marker.ownership.owner -ne 'engram' -or
        $marker.ownership.layout -ne 'generation-venv' -or $marker.manifest_sha256 -notmatch '^[a-f0-9]{64}$' -or
        $marker.package_version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $marker.registry_revision -ne 'external-overlay-components-v1') {
        throw 'Unrecognized generation ownership; preserved.'
    }
    foreach ($id in $marker.installed_components) {
        if ($id -notin @('bolttagu-2d','rabbit-2d','robot-arm','xeyes','robot-arm-3d','robot-arm-3d-v2','robot-arm-3d-v3')) { throw 'Invalid generation component ID.' }
    }
    [pscustomobject]@{ Root = $Root; Generation = $generation; Runtime = (Join-Path $generation 'venv'); Marker = $marker; Pointer = $pointer }
}

function Get-EngramComponentPlan {
    param([Parameter(Mandatory)][string]$ManifestPath, [string]$Components = '', [string[]]$Installed = @())
    $manifest = Read-EngramComponentJson $ManifestPath
    $expected = @('bolttagu-2d','rabbit-2d','robot-arm','xeyes','robot-arm-3d','robot-arm-3d-v2','robot-arm-3d-v3')
    $ids = @($manifest.components | ForEach-Object { [string]$_.id })
    if ($manifest.schema -ne 1 -or $manifest.package_version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or
        @($ids | Select-Object -Unique).Count -ne 7 -or @(Compare-Object $ids $expected).Count) {
        throw 'Unsupported external component manifest.'
    }
    $requested = @($Components.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $selected = @(@($requested) + @($Installed) | Sort-Object -Unique)
    foreach ($id in $selected) { if ($id -notin $ids) { throw "Unknown external component: $id" } }
    $payloads = @{}
    foreach ($payload in $manifest.payloads) {
        if ($payload.id -notmatch '^[a-z0-9][a-z0-9.-]*$' -or $payloads.ContainsKey([string]$payload.id)) { throw 'Invalid or duplicate resource ID.' }
        $payloads[[string]$payload.id] = $payload
    }
    $needed = New-Object 'System.Collections.Generic.HashSet[string]'
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    foreach ($component in $manifest.components) {
        if ($component.id -in $selected) { foreach ($id in $component.payloads) { $pending.Enqueue([string]$id) } }
    }
    while ($pending.Count) {
        $id = $pending.Dequeue()
        if (-not $payloads.ContainsKey($id)) { throw 'Missing component resource dependency.' }
        if ($needed.Add($id)) { foreach ($dependency in $payloads[$id].requires) { $pending.Enqueue([string]$dependency) } }
    }
    [pscustomobject]@{ Manifest = $manifest; ManifestPath = [IO.Path]::GetFullPath($ManifestPath); InstalledComponents = $selected; InstalledPayloads = @($needed | Sort-Object) }
}

function Test-EngramComponentArtifact {
    param([string]$BundleRoot, $Artifact)
    if ($Artifact.file -notmatch '^(payloads/)?[A-Za-z0-9][A-Za-z0-9_.-]*$' -or $Artifact.sha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw 'Invalid component artifact metadata.'
    }
    $path = Assert-EngramComponentPath $BundleRoot (Join-Path $BundleRoot $Artifact.file)
    if ((Get-EngramComponentHash $path) -ne $Artifact.sha256) { throw 'Component artifact digest mismatch.' }
    return $path
}

function Get-EngramInstalledComponents {
    param([string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'))
    $active = Get-EngramComponentGeneration $Root
    if ($active) { return [pscustomobject]@{ Ownership = 'generation'; InstalledComponents = @($active.Marker.installed_components); Runtime = $active.Runtime } }
    $runtime = Assert-EngramComponentPath $Root (Join-Path $Root 'runtime')
    if (-not (Test-Path -LiteralPath $runtime)) { return [pscustomobject]@{ Ownership = 'absent'; InstalledComponents = @(); Runtime = $runtime } }
    $all = @('bolttagu-2d','rabbit-2d','robot-arm','xeyes','robot-arm-3d','robot-arm-3d-v2','robot-arm-3d-v3')
    $fullPackage = $true
    foreach ($module in @('bolttagu_2d','rabbit_2d','robot_arm','robot_arm_3d','robot_arm_3d_v2','robot_arm_3d_v3','xeyes')) {
        $fullPackage = $fullPackage -and (Test-Path -LiteralPath (Join-Path $runtime ("Lib/site-packages/engram_overlay/overlays/$module.py")) -PathType Leaf)
    }
    $owned = $false
    try {
        $legacy = Read-EngramComponentJson (Join-Path $runtime '.installed-by-amber')
        $fields = @($legacy.PSObject.Properties.Name | Sort-Object)
        $expected = @('amber_version','autostart_value','installed_at','interpreter','overlay','overlay_version')
        $venv = Get-Content -LiteralPath (Join-Path $runtime 'pyvenv.cfg') -Raw
        $owned = @(Compare-Object $fields $expected).Count -eq 0 -and
            $legacy.amber_version -match '^\d+\.\d+\.\d+(\.\d+)?$' -and
            $legacy.overlay_version -match '^\d+\.\d+\.\d+(\.\d+)?$' -and
            $legacy.overlay -in @('', 'bolttagu-2d') -and
            [IO.Path]::IsPathRooted([string]$legacy.interpreter) -and
            $venv.Contains([string]$legacy.interpreter) -and
            ($legacy.autostart_value -eq '' -or $legacy.autostart_value -ceq ('"{0}" --provider' -f (Join-Path $runtime 'Scripts\engram-custom-overlayw.exe')))
        $parsedDate = [DateTimeOffset]::MinValue
        $owned = $owned -and [DateTimeOffset]::TryParse([string]$legacy.installed_at, [ref]$parsedDate)
        $metadata = Join-Path $runtime ('Lib/site-packages/engram_custom_overlay-' + $legacy.overlay_version + '.dist-info/METADATA')
        $owned = $owned -and (Test-Path -LiteralPath $metadata -PathType Leaf)
        if ($owned) {
            $metadataText = [IO.File]::ReadAllText($metadata)
            $owned = $metadataText -match '(?m)^Name: engram-custom-overlay\r?$' -and
                $metadataText -match ('(?m)^Version: ' + [regex]::Escape($legacy.overlay_version) + '\r?$')
            foreach ($module in @('bolttagu_2d','rabbit_2d','robot_arm','robot_arm_3d','robot_arm_3d_v2','robot_arm_3d_v3','xeyes')) {
                $owned = $owned -and (Test-Path -LiteralPath (Join-Path $runtime ("Lib/site-packages/engram_overlay/overlays/$module.py")) -PathType Leaf)
            }
        }
    } catch { $owned = $false }
    # Inventory is conservative: old full distributions advertise all seven.
    # An unrecognized installation is visible but never adopted or modified.
    [pscustomobject]@{ Ownership = $(if ($owned) { 'legacy-owned' } else { 'user-owned' }); InstalledComponents = $(if ($fullPackage) { $all } else { @() }); Runtime = $runtime }
}

function Assert-EngramComponentRuntimeInactive {
    param([string]$Runtime)
    $runtimePrefix = [IO.Path]::GetFullPath($Runtime).TrimEnd('\') + '\'
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($processes.Count) { throw 'External runtime is running. Close its provider before retrying component installation; current runtime and catalog are unchanged.' }
}

function Invoke-EngramExternalComponentInstallation {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ManifestPath, [Parameter(Mandatory)][string]$Components,
        [string]$PythonCommand = 'python', [string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'),
        [switch]$UpdateStartup, [string]$StateDirectory = (Join-Path $env:USERPROFILE '.engram'),
        [string]$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run')
    if (-not $Components.Trim()) { return [pscustomobject]@{ Installed = $false; Reason = 'no-components-selected' } }
    if ($UpdateStartup) { Resolve-EngramDeferredComponentStartup -Root $Root -StateDirectory $StateDirectory -RunKey $RunKey }
    $inventory = Get-EngramInstalledComponents $Root
    if ($UpdateStartup) { Repair-EngramComponentStartup -Root $Root -StateDirectory $StateDirectory -RunKey $RunKey }
    if ($inventory.Ownership -eq 'user-owned') {
        throw 'Existing legacy/user-owned external runtime preserved. Explicit migration is required before component installation.'
    }
    if ($inventory.Ownership -ne 'absent') {
        Assert-EngramComponentRuntimeInactive -Runtime $inventory.Runtime
    }
    # Never reinterpret a legacy or user-owned installation as generation ownership.
    $installed = @($inventory.InstalledComponents)
    Assert-EngramComponentPin $ManifestPath
    $plan = Get-EngramComponentPlan -ManifestPath $ManifestPath -Components $Components -Installed $installed
    $bundleRoot = Split-Path $plan.ManifestPath
    $wheel = Test-EngramComponentArtifact $bundleRoot $plan.Manifest.common
    foreach ($payload in $plan.Manifest.payloads) {
        if ($payload.id -in $plan.InstalledPayloads) { Test-EngramComponentArtifact $bundleRoot $payload | Out-Null }
    }
    $python = Resolve-EligiblePython -Command $PythonCommand
    if (-not $python.Ok) { throw "External component Python unavailable: $($python.Reason)" }
    $relative = 'generations/' + [Guid]::NewGuid().ToString('N')
    $generation = Assert-EngramComponentPath $Root (Join-Path $Root $relative)
    New-Item -ItemType Directory -Path $generation -Force | Out-Null
    $venv = Join-Path $generation 'venv'
    & $python.Executable -m venv $venv | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Component venv creation failed; previous generation remains active.' }
    $venvPython = Join-Path $venv 'Scripts/python.exe'
    & $venvPython -m pip install --disable-pip-version-check $wheel | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Component core installation failed; previous generation remains active.' }
    $markerPath = Join-Path $generation 'engram-overlay-components.json'
    & $venvPython -m engram_overlay.components install --manifest $plan.ManifestPath --payload-root $bundleRoot --components ($plan.InstalledComponents -join ',') --installed-marker $markerPath | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Component materialization failed; previous generation remains active.' }
    $marker = Read-EngramComponentJson $markerPath
    if ($marker.schema -ne 1 -or $marker.ownership.owner -ne 'engram' -or $marker.ownership.layout -ne 'generation-venv' -or
        $marker.package_version -ne $plan.Manifest.package_version -or $marker.registry_revision -ne $plan.Manifest.registry_revision -or
        $marker.manifest_sha256 -ne (Get-EngramComponentHash $plan.ManifestPath) -or
        @(Compare-Object @($marker.installed_components) @($plan.InstalledComponents)).Count -or
        @(Compare-Object @($marker.installed_payloads) @($plan.InstalledPayloads)).Count) { throw 'Component installation result does not match the verified plan.' }
    $pointer = @{ schema = 1; owner = 'engram'; generation = $relative; marker_sha256 = (Get-EngramComponentHash $markerPath) }
    $target = Join-Path $Root 'active-generation.json'
    $temporary = Join-Path $Root ('.active-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($pointer | ConvertTo-Json -Compress))
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    $startupChange = $null
    try {
        if ($inventory.Ownership -ne 'absent') { Assert-EngramComponentRuntimeInactive -Runtime $inventory.Runtime }
        if (-not $UpdateStartup -and $inventory.Ownership -ne 'absent') {
            Save-EngramDeferredComponentStartup -Root $Root -PreviousRuntime $inventory.Runtime -NextRuntime $venv
        }
        if ($UpdateStartup) { $startupChange = Update-EngramComponentStartup -PreviousRuntime $inventory.Runtime -NextRuntime $venv -StateDirectory $StateDirectory -RunKey $RunKey }
        if (Test-Path -LiteralPath $target) { [IO.File]::Replace($temporary, $target, (Join-Path $Root ('previous-' + [Guid]::NewGuid().ToString('N') + '.json'))) }
        else { [IO.File]::Move($temporary, $target) }
    } catch {
        if ($startupChange -and $startupChange.Changed) { Undo-EngramComponentStartup -Change $startupChange }
        throw
    }
    if ($startupChange -and $startupChange.Changed) {
        try { Complete-EngramComponentStartupJournal -Change $startupChange -Status 'committed' }
        catch { Write-Warning 'Generation published; startup journal completion pending. Next installation will reconcile the exact owned entry.' }
    }
    [pscustomobject]@{ Installed = $true; Reason = 'installed'; Runtime = $venv; InstalledComponents = $plan.InstalledComponents }
}

function Write-EngramComponentJsonAtomic {
    param([string]$Path, $Value)
    $pending = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 10))
    $stream = [IO.File]::Open($pending, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($pending, $Path, ($Path + '.' + [Guid]::NewGuid().ToString('N') + '.previous')) }
    else { [IO.File]::Move($pending, $Path) }
}

function Read-EngramDeferredComponentStartup {
    param([string]$Root)
    $path = Assert-EngramComponentPath $Root (Join-Path $Root 'deferred-startup.json')
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $record = Read-EngramComponentJson $path
    if ($record.schema -ne 1 -or $record.owner -ne 'engram-component-deferred-v1' -or
        $record.status -notin @('pending','complete','not-published') -or
        $record.previous -notmatch '^(runtime|generations/[a-f0-9]{32}/venv)$' -or
        $record.predecessor -notmatch '^(runtime|generations/[a-f0-9]{32}/venv)$' -or
        $record.next -notmatch '^generations/[a-f0-9]{32}/venv$' -or
        $record.previous_marker_sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Deferred external startup metadata invalid; preserved.' }
    return $record
}

function Save-EngramDeferredComponentStartup {
    param([string]$Root, [string]$PreviousRuntime, [string]$NextRuntime)
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $previous = (Assert-EngramComponentPath $Root $PreviousRuntime).Substring($base.Length + 1).Replace('\','/')
    $predecessor = $previous
    $next = (Assert-EngramComponentPath $Root $NextRuntime).Substring($base.Length + 1).Replace('\','/')
    $old = Read-EngramDeferredComponentStartup $Root
    if ($old -and $old.status -eq 'pending' -and
        ($old.next -eq $previous -or $old.predecessor -eq $previous)) {
        # A journaled candidate may never have become active. A subsequent
        # NoStart upgrade from that candidate's still-active predecessor must
        # retain the original login target, not replace it with the predecessor.
        $previous = $old.previous
        $digest = $old.previous_marker_sha256
    } else {
        $marker = if ($previous -eq 'runtime') { Join-Path $PreviousRuntime '.installed-by-amber' } else { Join-Path (Split-Path $PreviousRuntime) 'engram-overlay-components.json' }
        $digest = Get-EngramComponentHash (Assert-EngramComponentPath $Root $marker)
    }
    $record = @{ schema=1; owner='engram-component-deferred-v1'; status='pending'; previous=$previous; predecessor=$predecessor; next=$next; previous_marker_sha256=$digest }
    Write-EngramComponentJsonAtomic -Path (Join-Path $Root 'deferred-startup.json') -Value $record
    Write-Host 'Login registration unchanged. The next normal installer/dev startup pass will reconcile only the proven owned entry.'
}

function Resolve-EngramDeferredComponentStartup {
    param([string]$Root, [string]$StateDirectory, [string]$RunKey)
    $record = Read-EngramDeferredComponentStartup $Root
    if (-not $record -or $record.status -ne 'pending') { return }
    $previous = Assert-EngramComponentPath $Root (Join-Path $Root $record.previous)
    $next = Assert-EngramComponentPath $Root (Join-Path $Root $record.next)
    $active = Get-EngramComponentGeneration $Root
    if (-not $active) { return }
    if ([IO.Path]::GetFullPath($active.Runtime) -ne $next) {
        # A second NoStart upgrade can stop after journaling but before publishing.
        # Its captured predecessor is still the verified active generation; heal
        # the earlier successful upgrade, never the unpublished candidate.
        $predecessor = Assert-EngramComponentPath $Root (Join-Path $Root $record.predecessor)
        if ([IO.Path]::GetFullPath($active.Runtime) -ne $predecessor -or $predecessor -eq $previous) { return }
        $next = $predecessor
        $record.next = $record.predecessor
    }
    $marker = if ($record.previous -eq 'runtime') { Join-Path $previous '.installed-by-amber' } else { Join-Path (Split-Path $previous) 'engram-overlay-components.json' }
    if ((Get-EngramComponentHash (Assert-EngramComponentPath $Root $marker)) -cne $record.previous_marker_sha256) {
        throw 'Deferred startup origin marker changed; login registration preserved.'
    }
    Repair-EngramComponentStartup -Root $Root -StateDirectory $StateDirectory -RunKey $RunKey
    $change = Update-EngramComponentStartup -PreviousRuntime $previous -NextRuntime $next -StateDirectory $StateDirectory -RunKey $RunKey
    if ($change.Changed) { Complete-EngramComponentStartupJournal -Change $change -Status 'committed' }
    $record.status = 'complete'
    Write-EngramComponentJsonAtomic -Path (Join-Path $Root 'deferred-startup.json') -Value $record
}

function Install-EngramExternalComponents {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ManifestPath, [Parameter(Mandatory)][string]$Components,
        [string]$PythonCommand = 'python', [string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'),
        [switch]$UpdateStartup, [string]$StateDirectory = (Join-Path $env:USERPROFILE '.engram'),
        [string]$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run')
    if (-not $Components.Trim()) { return [pscustomobject]@{ Installed = $false; Reason = 'no-components-selected' } }
    $validatedRoot = Assert-EngramComponentPath $Root $Root
    New-Item -ItemType Directory -Path $validatedRoot -Force | Out-Null
    $lock = [IO.File]::Open((Join-Path $validatedRoot '.component-install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { Invoke-EngramExternalComponentInstallation -ManifestPath $ManifestPath -Components $Components -PythonCommand $PythonCommand -Root $Root -UpdateStartup:$UpdateStartup -StateDirectory $StateDirectory -RunKey $RunKey }
    finally { $lock.Dispose() }
}

function Update-EngramComponentStartup {
    param([string]$PreviousRuntime, [string]$NextRuntime, [string]$StateDirectory, [string]$RunKey)
    $current = Get-ItemProperty -LiteralPath $RunKey -Name EngramOverlay -ErrorAction SilentlyContinue
    if (-not $current) { return [pscustomobject]@{ Changed = $false } }
    $previous = '"{0}" --provider' -f (Join-Path $PreviousRuntime 'Scripts\engram-custom-overlayw.exe')
    $next = '"{0}" --provider' -f (Join-Path $NextRuntime 'Scripts\engram-custom-overlayw.exe')
    $recordPath = Join-Path $StateDirectory 'joint-startup.json'
    $record = if (Test-Path -LiteralPath $recordPath) { Read-EngramComponentJson $recordPath } else { $null }
    if ($current.EngramOverlay -cne $previous -or -not $record -or $record.owner -ne 'engram-joint-startup-v1' -or $record.external -cne $previous) {
        Write-Warning 'External login entry is user-owned/unrecognized; preserved. Update it manually to use the newly installed generation.'
        return [pscustomobject]@{ Changed = $false }
    }
    $original = [IO.File]::ReadAllText($recordPath)
    $record.external = $next
    $updated = $record | ConvertTo-Json -Depth 8
    # The journal makes the exact prior owned value recoverable after interruption.
    $journal = Join-Path $StateDirectory ('component-startup-' + [Guid]::NewGuid().ToString('N') + '.json')
    $change = [pscustomobject]@{ owner = 'engram-component-startup-v1'; status = 'pending'; JournalPath = $journal; Changed = $true; RunKey = $RunKey; Previous = $previous; Next = $next; RecordPath = $recordPath; Original = $original; Updated = $updated }
    [IO.File]::WriteAllText($journal, ($change | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    try {
        Set-ItemProperty -LiteralPath $RunKey -Name EngramOverlay -Value $next -ErrorAction Stop
        [IO.File]::WriteAllText($recordPath, $updated, [Text.UTF8Encoding]::new($false))
    } catch { Undo-EngramComponentStartup -Change $change; throw }
    return $change
}

function Undo-EngramComponentStartup {
    param($Change)
    $current = Get-ItemProperty -LiteralPath $Change.RunKey -Name EngramOverlay -ErrorAction Stop
    if ($current.EngramOverlay -ceq $Change.Next) {
        Set-ItemProperty -LiteralPath $Change.RunKey -Name EngramOverlay -Value $Change.Previous -ErrorAction Stop
    }
    $text = [IO.File]::ReadAllText($Change.RecordPath)
    if ($text -ceq $Change.Updated) { [IO.File]::WriteAllText($Change.RecordPath, $Change.Original, [Text.UTF8Encoding]::new($false)) }
    Complete-EngramComponentStartupJournal -Change $Change -Status 'rolled-back'
}

function Complete-EngramComponentStartupJournal {
    param($Change, [string]$Status)
    $Change.status = $Status
    [IO.File]::WriteAllText($Change.JournalPath, ($Change | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
}

function Repair-EngramComponentStartup {
    param([string]$Root, [string]$StateDirectory, [string]$RunKey)
    if (-not (Test-Path -LiteralPath $StateDirectory)) { return }
    foreach ($file in Get-ChildItem -LiteralPath $StateDirectory -Filter 'component-startup-*.json' -File) {
        $change = Read-EngramComponentJson $file.FullName
        if ($change.owner -ne 'engram-component-startup-v1' -or $change.status -ne 'pending') { continue }
        $recordPath = Join-Path $StateDirectory 'joint-startup.json'
        if ($change.JournalPath -cne $file.FullName -or $change.RecordPath -cne $recordPath -or $change.RunKey -cne $RunKey -or
            $change.Next -notmatch '^"(.+)" --provider$') { throw 'Interrupted component startup journal cannot be safely recovered; preserved.' }
        $next = Assert-EngramComponentPath $Root $Matches[1]
        $relative = $next.Substring([IO.Path]::GetFullPath($Root).TrimEnd('\').Length).Replace('\','/')
        if ($relative -notmatch '^/generations/[a-f0-9]{32}/venv/Scripts/engram-custom-overlayw.exe$') { throw 'Invalid generation in startup journal.' }
        $before = $change.Original | ConvertFrom-Json
        $after = $change.Updated | ConvertFrom-Json
        if ($before.owner -ne 'engram-joint-startup-v1' -or $after.owner -ne $before.owner -or
            $before.host -cne $after.host -or $before.arguments -cne $after.arguments -or
            $before.external -cne $change.Previous -or $after.external -cne $change.Next) { throw 'Interrupted startup provenance mismatch; preserved.' }
        $active = Get-EngramComponentGeneration $Root
        $activeValue = if ($active) { '"{0}" --provider' -f (Join-Path $active.Runtime 'Scripts\engram-custom-overlayw.exe') } else { '' }
        if ($activeValue -ceq $change.Next) { Complete-EngramComponentStartupJournal -Change $change -Status 'committed' }
        else { Undo-EngramComponentStartup -Change $change }
    }
}

function Install-EngramOverlaySdk {
    param([Parameter(Mandatory)][string]$ManifestPath, [string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'))
    Assert-EngramComponentPin $ManifestPath
    $plan = Get-EngramComponentPlan -ManifestPath $ManifestPath
    $artifact = Test-EngramComponentArtifact (Split-Path $plan.ManifestPath) $plan.Manifest.sdk
    $destination = Assert-EngramComponentPath $Root (Join-Path $Root ('sdk/' + $plan.Manifest.sdk.sha256))
    if (Test-Path -LiteralPath $destination) {
        $marker = Read-EngramComponentJson (Join-Path $destination '.engram-sdk.json')
        if ($marker.owner -ne 'engram-sdk-v1' -or $marker.sha256 -ne $plan.Manifest.sdk.sha256) { throw 'SDK destination is user-owned; preserved.' }
        return $destination
    }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $staging = Assert-EngramComponentPath $Root (Join-Path $Root ('sdk/.staging-' + [Guid]::NewGuid().ToString('N')))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    $archive = [IO.Compression.ZipFile]::OpenRead($artifact)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $size = 0L
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if ($name -notmatch '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$' -or $name.Split('/') -contains '..' -or
                -not $seen.Add($name) -or $entry.Length -gt 10485760) { throw 'Unsafe SDK archive entry.' }
            $size += $entry.Length
            if ($size -gt 20971520 -or $seen.Count -gt 512) { throw 'SDK archive exceeds bounded size.' }
            $target = Assert-EngramComponentPath $staging (Join-Path $staging $name)
            New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
        }
    } finally { $archive.Dispose() }
    $marker = @{ owner = 'engram-sdk-v1'; sha256 = $plan.Manifest.sdk.sha256 }
    [IO.File]::WriteAllText((Join-Path $staging '.engram-sdk.json'), ($marker | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    [IO.Directory]::Move($staging, $destination)
    return $destination
}
