param(
    [Parameter(Mandatory)][string]$ProjectRoot,
    [Parameter(Mandatory)][string]$UserProfile,
    [ValidateSet("All", "Claude", "Copilot", "Codex")][string]$Provider = "All"
)

$agentsSourceDir = Join-Path $ProjectRoot "config\agents"
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
        Copy-Item -LiteralPath $source -Destination $destination -Force
        Write-Output $destination
    }
}
