#
# Deploy provider-owned planner/coder/servant definitions.
#
# Each provider keeps its own format and destination.
#
# Ownership rule: update only definitions that still match our recorded hash.
# User-authored or user-modified files are preserved.
#
# -Force overwrites only after creating an .engram-bak backup.

param(
    [Parameter(Mandatory)][string]$ProjectRoot,
    [Parameter(Mandatory)][string]$UserProfile,
    [ValidateSet("All", "Claude", "Copilot", "Codex")][string]$Provider = "All",
    [switch]$Force
)

$agentsSourceDir = Join-Path $ProjectRoot "config\agents"
$provenancePath = Join-Path $UserProfile ".engram\agent-definitions.json"
$providers = @(
    @{
        Key = "Claude"
        Name = "Claude Code"
        SourceDir = Join-Path $agentsSourceDir "claude"
        DestinationDir = Join-Path $UserProfile ".claude\agents"
        Extension = ".md"
    },
    @{
        Key = "Copilot"
        Name = "Copilot CLI"
        SourceDir = Join-Path $agentsSourceDir "copilot"
        DestinationDir = Join-Path $UserProfile ".copilot\agents"
        Extension = ".agent.md"
    },
    @{
        Key = "Codex"
        Name = "Codex"
        SourceDir = Join-Path $agentsSourceDir "codex"
        DestinationDir = Join-Path $UserProfile ".codex\agents"
        Extension = ".toml"
    }
)

if ($Provider -ne "All") {
    $providers = @($providers | Where-Object { $_.Key -eq $Provider })
}

function Get-ContentHash([string]$Path) {
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

function Read-Provenance([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        return @{}
    }
    $map = @{}
    foreach ($property in $raw.PSObject.Properties) { $map[$property.Name] = [string]$property.Value }
    return $map
}

$provenance = Read-Provenance $provenancePath
$updated = @{}
foreach ($key in $provenance.Keys) { $updated[$key] = $provenance[$key] }

foreach ($providerSpec in $providers) {
    if (-not (Test-Path -LiteralPath $providerSpec.DestinationDir)) {
        New-Item -Path $providerSpec.DestinationDir -ItemType Directory -Force | Out-Null
    }

    foreach ($role in @("planner", "coder", "servant")) {
        $source = Join-Path $providerSpec.SourceDir ($role + $providerSpec.Extension)
        $destination = Join-Path $providerSpec.DestinationDir ($role + $providerSpec.Extension)
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Managed $($providerSpec.Name) agent source not found: $source"
        }

        $currentHash = Get-ContentHash $destination
        $sourceHash = Get-ContentHash $source
        $recordedHash = if ($provenance.ContainsKey($destination)) { $provenance[$destination] } else { "" }

        # Adopt an identical deployed file when its provenance record predates tracking.
        if ($currentHash -and -not $recordedHash -and $currentHash -eq $sourceHash) {
            $recordedHash = $currentHash
        }

        if ($currentHash -and $currentHash -ne $recordedHash) {
            # The file belongs to the user or another tool.
            if (-not $Force) {
                Write-Output "SKIP  $destination (user-owned or modified)"
                continue
            }
            $backup = "$destination.engram-bak"
            Copy-Item -LiteralPath $destination -Destination $backup -Force
            Write-Output "BACKUP $backup"
        }

        Copy-Item -LiteralPath $source -Destination $destination -Force
        $updated[$destination] = Get-ContentHash $destination
        Write-Output $destination
    }
}

New-Item -Path (Split-Path $provenancePath) -ItemType Directory -Force | Out-Null
($updated | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $provenancePath -Encoding UTF8
