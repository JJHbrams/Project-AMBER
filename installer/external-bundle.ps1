# Reproducible component acquisition: one trusted repository, exact committed
# version and manifest SHA, then per-artifact SHA. Never accepts a caller URL.
. (Join-Path $PSScriptRoot 'external-components.ps1')
function Receive-EngramPinnedArtifact {
    param([string]$Version, [string]$FileName, [string]$Digest, [string]$Destination)
    if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $FileName -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$' -or $Digest -notmatch '^[a-f0-9]{64}$') { throw 'Invalid pinned artifact request.' }
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-EngramComponentHash $Destination) -cne $Digest) { throw 'Cached component artifact differs from pin; preserved for inspection.' }
        return
    }
    New-Item -ItemType Directory -Path (Split-Path $Destination) -Force | Out-Null
    $pending = $Destination + '.' + [Guid]::NewGuid().ToString('N') + '.download'
    try {
        $url = "https://github.com/JJHbrams/engram-overlay/releases/download/v$Version/$FileName"
        Invoke-WebRequest -Uri $url -OutFile $pending -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
        if ((Get-EngramComponentHash $pending) -cne $Digest) { throw 'Downloaded component artifact digest mismatch.' }
        [IO.File]::Move($pending, $Destination)
    } finally {
        if (Test-Path -LiteralPath $pending -PathType Leaf) { Remove-Item -LiteralPath $pending -Force }
    }
}

function Resolve-EngramComponentBundle {
    param([string]$Destination = (Join-Path $PSScriptRoot 'external-components'), [string]$Components = '',
        [switch]$Sdk, [switch]$All, [switch]$NoDownload)
    $destinationRoot = Assert-EngramComponentPath $Destination $Destination
    $manifestPath = Join-Path $destinationRoot 'engram-overlay-components.json'
    $version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'external-overlay.pin') -Raw).Trim()
    $digest = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'external-components.pin') -Raw).Trim()
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        if ($NoDownload) { throw 'Pinned component manifest unavailable offline.' }
        Receive-EngramPinnedArtifact -Version $version -FileName 'engram-overlay-components.json' -Digest $digest -Destination $manifestPath
    }
    Assert-EngramComponentPin $manifestPath
    $plan = Get-EngramComponentPlan -ManifestPath $manifestPath -Components $Components
    if ($plan.Manifest.package_version -cne $version) { throw 'Component manifest version differs from installer pin.' }
    if ($All) { $plan = Get-EngramComponentPlan -ManifestPath $manifestPath -Components (@($plan.Manifest.components | ForEach-Object { $_.id }) -join ',') }
    $artifacts = @()
    if ($plan.InstalledComponents.Count) { $artifacts += $plan.Manifest.common }
    if ($Sdk -or $All) { $artifacts += $plan.Manifest.sdk }
    $artifacts += @($plan.Manifest.payloads | Where-Object { $_.id -in $plan.InstalledPayloads })
    foreach ($artifact in $artifacts) {
        if ($artifact.file -notmatch '^(payloads/)?[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw 'Unsafe component artifact name.' }
        $path = Assert-EngramComponentPath $destinationRoot (Join-Path $destinationRoot $artifact.file)
        if (-not (Test-Path -LiteralPath $path)) {
            if ($NoDownload) { throw 'Selected component artifact unavailable offline.' }
            Receive-EngramPinnedArtifact -Version $version -FileName (Split-Path $artifact.file -Leaf) -Digest $artifact.sha256 -Destination $path
        }
        Test-EngramComponentArtifact $destinationRoot $artifact | Out-Null
    }
    return $manifestPath
}
