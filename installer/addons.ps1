#
# Shared addon installer used by both installer entry points: the frozen
# installer (installer\configure.ps1, only $InstallDir exists) and the
# source-tree entry point (installer\modules\07_shims.ps1, which has
# $ProjectRoot). Every filesystem path this file needs comes in as a
# parameter -- nothing is inferred from $PSScriptRoot or a project layout.
#
# Ownership rule (re-implemented from installer\deploy_agent_definitions.ps1,
# not reused as a function: that file hardcodes provider/role tables that do
# not apply here). It exists because an earlier version of this project did
# Copy-Item -Force over user-authored planner/coder/servant agent
# definitions with no backup and no warning, and had to be hotfixed twice
# (v1.5.13, v1.5.14). The same mistake must not happen to addon skills:
#
#   - destination missing, or present but matches our recorded hash
#     -> update silently (idempotent)
#   - destination present, no provenance record, but byte-identical to the
#     payload we would install -> adopt it (record the hash), then update
#   - destination present and its content differs from both our record and
#     our payload -> it is user-owned; SKIP and say why
#   - -Force -> back up the mismatched file to <file>.engram-bak, then
#     overwrite
#
# Every branch below ends in a sentence written to the console. A silent
# fallback is exactly the failure mode this file exists to avoid.

function Read-EngramAddonMeta {
    # installer\addons.pin is the single source of truth for an addon's skill
    # directory name and MCP registration shape. It is not shipped as-is;
    # build-addons.ps1 bakes it into ".addon-meta.json" at the root of each
    # addon's own zip. That is the only place either Install-EngramAddons or
    # Register-EngramAddonMcp look: the real install path
    # ({tmp}\addon-bundle, staged by PrepareAddonBundle()) never carries a
    # sibling addons.json, so metadata that lived outside the zip could --
    # and did -- go missing while the payload it described still installed.
    # A missing/unreadable meta file is reported, not silently defaulted:
    # the caller decides what identity fallback (if any) is safe.
    param([Parameter(Mandatory)][string]$ZipPath)
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { return $null }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = $null
    try {
        $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
        $entry = $archive.GetEntry('.addon-meta.json')
        if (-not $entry) { return $null }
        $reader = New-Object IO.StreamReader($entry.Open())
        try { $raw = $reader.ReadToEnd() } finally { $reader.Dispose() }
        return ($raw | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        return $null
    } finally {
        if ($archive) { $archive.Dispose() }
    }
}

function Get-EngramAddonFileHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "")
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Read-EngramAddonProvenance {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        return @{}
    }
    $map = @{}
    if ($raw) {
        foreach ($property in $raw.PSObject.Properties) { $map[$property.Name] = [string]$property.Value }
    }
    return $map
}

<#
.SYNOPSIS
    Installs one or more optional addon skills from zip payloads.

.PARAMETER PayloadRoot
    Directory that holds one zip per addon, named "<Addon>.zip" where
    <Addon> is the same key passed in -Addons (e.g. "claude-image-forge.zip").

.PARAMETER Addons
    Addon keys to install, e.g. @("claude-image-forge", "session-agent-orchestration").

.PARAMETER UserProfile
    Destination profile root. Defaults to $env:USERPROFILE. Tests pass a
    temporary profile so they never touch a real one.

.PARAMETER Force
    Overwrite a user-owned or user-modified file after backing it up to
    "<file>.engram-bak".
#>
function Install-EngramAddons {
    param(
        [Parameter(Mandatory)][string]$PayloadRoot,
        [Parameter(Mandatory)][string[]]$Addons,
        [string]$UserProfile = $env:USERPROFILE,
        [switch]$Force
    )

    $provenancePath = Join-Path $UserProfile ".engram\addon-definitions.json"
    $provenance = Read-EngramAddonProvenance $provenancePath
    $updated = @{}
    foreach ($key in $provenance.Keys) { $updated[$key] = $provenance[$key] }

    foreach ($addon in $Addons) {
        $zipPath = Join-Path $PayloadRoot ($addon + ".zip")
        $zipInfo = Get-Item -LiteralPath $zipPath -ErrorAction SilentlyContinue

        if (-not $zipInfo -or $zipInfo.Length -eq 0) {
            Write-Output "SKIP  ${addon}: 이 번들에 포함되지 않았습니다 ($zipPath)"
            continue
        }

        $meta = Read-EngramAddonMeta -ZipPath $zipPath
        $skillName = if ($meta -and $meta.skill_name) { [string]$meta.skill_name } else { $addon }
        if (-not $meta -or -not $meta.skill_name) {
            Write-Output "WARN  ${addon}: 번들 안에서 .addon-meta.json 을 찾지 못해 addon id를 스킬 이름으로 사용합니다 ($skillName)"
        }

        $destinationDir = Join-Path $UserProfile (".claude\skills\" + $skillName)
        if (-not (Test-Path -LiteralPath $destinationDir)) {
            New-Item -Path $destinationDir -ItemType Directory -Force | Out-Null
        }

        $stagingDir = Join-Path ([IO.Path]::GetTempPath()) ("engram-addon-" + [Guid]::NewGuid().ToString("N"))
        New-Item -Path $stagingDir -ItemType Directory -Force | Out-Null
        try {
            Expand-Archive -LiteralPath $zipPath -DestinationPath $stagingDir -Force

            $sourceFiles = @(Get-ChildItem -LiteralPath $stagingDir -Recurse -File |
                Where-Object { $_.Name -ne '.addon-meta.json' })
            $installedCount = 0
            $skippedCount = 0

            foreach ($sourceFile in $sourceFiles) {
                $relativePath = $sourceFile.FullName.Substring($stagingDir.Length).TrimStart('\', '/')
                $destination = Join-Path $destinationDir $relativePath
                $destinationParent = Split-Path $destination
                if ($destinationParent -and -not (Test-Path -LiteralPath $destinationParent)) {
                    New-Item -Path $destinationParent -ItemType Directory -Force | Out-Null
                }

                $currentHash = Get-EngramAddonFileHash $destination
                $sourceHash = Get-EngramAddonFileHash $sourceFile.FullName
                $recordedHash = if ($provenance.ContainsKey($destination)) { $provenance[$destination] } else { "" }

                # Adopt an identical deployed file when its provenance record predates tracking.
                if ($currentHash -and -not $recordedHash -and $currentHash -eq $sourceHash) {
                    $recordedHash = $currentHash
                }

                if ($currentHash -and $currentHash -ne $recordedHash) {
                    # The file belongs to the user or another tool.
                    if (-not $Force) {
                        Write-Output "SKIP  $destination (사용자가 만들었거나 손댄 파일이라 건드리지 않습니다)"
                        $skippedCount++
                        continue
                    }
                    $backup = "$destination.engram-bak"
                    Copy-Item -LiteralPath $destination -Destination $backup -Force
                    Write-Output "BACKUP $backup"
                }

                Copy-Item -LiteralPath $sourceFile.FullName -Destination $destination -Force
                $updated[$destination] = Get-EngramAddonFileHash $destination
                $installedCount++
                Write-Output $destination
            }

            Write-Output "$addon : $skillName 에 $installedCount 개 설치, $skippedCount 개 건너뜀 ($destinationDir)"
        } finally {
            Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $provenanceDir = Split-Path $provenancePath
    if ($provenanceDir -and -not (Test-Path -LiteralPath $provenanceDir)) {
        New-Item -Path $provenanceDir -ItemType Directory -Force | Out-Null
    }
    ($updated | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $provenancePath -Encoding UTF8
}

<#
.SYNOPSIS
    Registers the MCP servers that image-forge and session-agent-orchestration
    need, using the same JSON-merge shape as installer\configure.ps1's
    Merge-JsonMcp (see lines 380-402), but never overwriting an existing
    entry: unlike the shared "engram" entry that function manages, these are
    optional third-party servers and a name collision means the user already
    has something registered under that name.

    Deliberately does not run any addon's own install.ps1 / install.mjs --
    running third-party code at install time is out of scope by decision.
    The command/args come from each addon's ".addon-meta.json" (baked in by
    build-addons.ps1 from installer\addons.pin), the same file
    Install-EngramAddons reads for the skill directory name -- one source,
    so the registered path can never point at a directory the install step
    did not actually create.

.PARAMETER PayloadRoot
    Directory that holds one zip per addon, named "<Addon>.zip". Same
    contract as Install-EngramAddons's -PayloadRoot.

.PARAMETER Addons
    Addon keys to register, e.g. @("claude-image-forge", "session-agent-orchestration").
    Addons that need no MCP server are reported and skipped.

.PARAMETER UserProfile
    Destination profile root. Defaults to $env:USERPROFILE.
#>
function Test-EngramAddonMcpPrerequisite {
    <#
    .SYNOPSIS
        Checks whether an MCP server's launch command actually resolves on
        this machine before we write a dead entry into ~/.claude.json.

        AMBER Model B's entire reason to exist is "no conda/python required
        on the install user's machine." session-orchestrator-mcp is a pip
        console script (only present because this dev machine has conda);
        node is an external prerequisite AMBER does not bundle. Neither
        belongs on a clean install machine by default, so both must be
        verified with Get-Command, not assumed.

    .OUTPUTS
        Hashtable with Ok (bool) and Reason (string, only set when not Ok).
    #>
    param([Parameter(Mandatory)][string]$Command, [string]$MinNodeVersion = "18")

    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $resolved) {
        return @{ Ok = $false; Reason = "명령 `"$Command`" 을 찾을 수 없습니다" }
    }
    if ($Command -eq "node") {
        try {
            $versionOutput = & node --version 2>$null
            $versionText = ([string]$versionOutput).Trim().TrimStart('v')
            $major = [int]($versionText.Split('.')[0])
        } catch {
            return @{ Ok = $false; Reason = "node 버전을 확인할 수 없습니다" }
        }
        if ($major -lt [int]$MinNodeVersion) {
            return @{ Ok = $false; Reason = "node $versionText 는 너무 낮습니다 (>= $MinNodeVersion 필요)" }
        }
    }
    return @{ Ok = $true; Reason = "" }
}

function Register-EngramAddonMcp {
    param(
        [Parameter(Mandatory)][string]$PayloadRoot,
        [Parameter(Mandatory)][string[]]$Addons,
        [string]$UserProfile = $env:USERPROFILE
    )

    $configPath = Join-Path $UserProfile ".claude.json"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    foreach ($addon in $Addons) {
        $zipPath = Join-Path $PayloadRoot ($addon + ".zip")
        $meta = Read-EngramAddonMeta -ZipPath $zipPath
        if (-not $meta) {
            Write-Output "SKIP  ${addon}: .addon-meta.json 을 읽을 수 없어 MCP 등록 여부를 판단할 수 없습니다 ($zipPath)"
            continue
        }
        if (-not $meta.needs_mcp) {
            Write-Output "SKIP  ${addon}: MCP 서버 등록이 필요 없는 addon 입니다"
            continue
        }
        if (-not $meta.mcp -or -not $meta.mcp.server_name -or -not $meta.mcp.command) {
            Write-Output "SKIP  ${addon}: needs_mcp=true 이지만 .addon-meta.json 에 mcp 스펙이 없어 등록하지 않습니다"
            continue
        }
        $skillName = if ($meta.skill_name) { [string]$meta.skill_name } else { $addon }
        $skillDir = Join-Path $UserProfile (".claude\skills\" + $skillName)
        $args = @(@($meta.mcp.args) | ForEach-Object {
            if ($_) { Join-Path $skillDir ([string]$_) } else { $_ }
        })
        $spec = @{
            ServerName = [string]$meta.mcp.server_name
            Prerequisite = [string]$meta.mcp.prerequisite
            PrerequisiteLabel = [string]$meta.mcp.prerequisite_label
            Entry = @{
                type    = "stdio"
                command = [string]$meta.mcp.command
                args    = $args
            }
        }
        $prereq = Test-EngramAddonMcpPrerequisite -Command $spec.Prerequisite
        if (-not $prereq.Ok) {
            Write-Output "SKIP  $($spec.ServerName): 스킬은 설치했으나 MCP 서버 $($spec.ServerName) 은 $($spec.PrerequisiteLabel) 가 없어 등록하지 않았습니다 ($($prereq.Reason)). $($spec.PrerequisiteLabel) 설치 후 다시 실행하면 등록됩니다."
            continue
        }
        try {
            $dir = Split-Path $configPath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -Path $dir -ItemType Directory -Force | Out-Null
            }
            $root = if (Test-Path -LiteralPath $configPath) {
                try { Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json } catch { [PSCustomObject]@{} }
            } else {
                [PSCustomObject]@{}
            }
            if (-not $root.PSObject.Properties["mcpServers"]) {
                $root | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([PSCustomObject]@{}) -Force
            }
            if ($root.mcpServers.PSObject.Properties[$spec.ServerName]) {
                Write-Output "SKIP  $($spec.ServerName): 이미 등록된 MCP 서버라 덮어쓰지 않습니다 ($configPath)"
                continue
            }
            $root.mcpServers | Add-Member -NotePropertyName $spec.ServerName -NotePropertyValue ([PSCustomObject]$spec.Entry) -Force
            [System.IO.File]::WriteAllText($configPath, ($root | ConvertTo-Json -Depth 12), $utf8NoBom)
            Write-Output "$($spec.ServerName): MCP 서버로 등록했습니다 ($configPath)"
        } catch {
            Write-Output "SKIP  $($spec.ServerName): MCP 등록 중 오류로 건너뜁니다 ($_)"
        }
    }
}
