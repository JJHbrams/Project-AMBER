# Shared pinned wheel resolver. Only the repository's trusted release URL is used.
function Resolve-ExternalOverlayWheel {
    param([ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')][string]$Version,
          [string]$ProjectRoot = (Split-Path $PSScriptRoot), [switch]$NoDownload)
    $name = "engram_custom_overlay-$Version-py3-none-any.whl"
    $cacheDir = Join-Path $ProjectRoot 'build\external-overlay'
    $cached = Join-Path $cacheDir $name
    if (Test-Path -LiteralPath $cached -PathType Leaf) { return $cached }
    $roots = @((Split-Path $ProjectRoot))
    $git = Join-Path $ProjectRoot '.git'
    if (Test-Path -LiteralPath $git -PathType Leaf) {
        $pointer = (Get-Content -LiteralPath $git -Raw).Trim()
        if ($pointer -match '^gitdir:\s*(.+)$') {
            $git = if ([IO.Path]::IsPathRooted($Matches[1])) { $Matches[1] } else { Join-Path $ProjectRoot $Matches[1] }
            $common = Join-Path $git 'commondir'
            if (Test-Path -LiteralPath $common) {
                $commonPath = [IO.Path]::GetFullPath((Join-Path $git (Get-Content -LiteralPath $common -Raw).Trim()))
                $roots += Split-Path (Split-Path $commonPath)
            }
        }
    }
    foreach ($root in $roots) {
        $sibling = Join-Path $root "engram-overlay\dist\$name"
        if (Test-Path -LiteralPath $sibling -PathType Leaf) {
            New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
            Copy-Item -LiteralPath $sibling -Destination $cached
            return $cached
        }
    }
    if ($NoDownload) { return '' }
    New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
    $pending = Join-Path $cacheDir ($name + '.' + [Guid]::NewGuid().ToString('N') + '.download')
    try {
        $url = "https://github.com/JJHbrams/engram-overlay/releases/download/v$Version/$name"
        Invoke-WebRequest -Uri $url -OutFile $pending -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
        Move-Item -LiteralPath $pending -Destination $cached
        return $cached
    } catch {
        Write-Warning "Could not acquire pinned external renderer $Version. Supply the verified wheel in build/external-overlay or retry online."
        return ''
    } finally {
        if (Test-Path -LiteralPath $pending -PathType Leaf) { Remove-Item -LiteralPath $pending -Force }
    }
}
