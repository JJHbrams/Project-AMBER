#Requires -Version 5

function Get-EngramSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-EngramInstallerInputFiles([string]$Root, [string]$DistDir) {
    $files = [System.Collections.Generic.List[string]]::new()
    foreach ($relative in @(
        "installer\engram-overlay.iss",
        "installer\configure.ps1",
        "installer\stop-engram-processes.ps1",
        "config\overlay.yaml",
        "config\config.yaml",
        "config\clients\copilot.md",
        ".github\skills\engram\SKILL.md",
        ".github\skills\orchestrate\SKILL.md",
        ".github\skills\engram-new-session\SKILL.md",
        ".github\skills\engram-task-workflow\SKILL.md",
        ".github\skills\engram-wiki-workflow\SKILL.md",
        ".github\skills\engram-close-session\SKILL.md"
    )) {
        $path = Join-Path $Root $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) { $files.Add($path) }
    }
    $templateDir = Join-Path $Root "installer\templates"
    if (Test-Path -LiteralPath $templateDir) {
        Get-ChildItem -LiteralPath $templateDir -File -Recurse |
            ForEach-Object { $files.Add($_.FullName) }
    }
    $buildManifest = Join-Path $DistDir "build-manifest.json"
    if (Test-Path -LiteralPath $buildManifest -PathType Leaf) {
        $files.Add($buildManifest)
    }
    return @($files | Sort-Object -Unique)
}

function Get-EngramInstallerSignature(
    [string]$Root,
    [string]$DistDir,
    [string]$BuildProfile
) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $entries = [System.Collections.Generic.List[string]]::new()
    $entries.Add("profile=$BuildProfile")
    foreach ($path in Get-EngramInstallerInputFiles $Root $DistDir) {
        $fullPath = [IO.Path]::GetFullPath($path)
        $relative = if ($fullPath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) {
            $fullPath.Substring($rootPath.Length).TrimStart('\').Replace('\', '/')
        } else {
            "dist/build-manifest.json"
        }
        $entries.Add("$relative=$(Get-EngramSha256 $fullPath)")
    }
    $payload = [Text.Encoding]::UTF8.GetBytes(($entries -join "`n"))
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hasher.ComputeHash($payload))).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Test-EngramInstallerCache(
    [string]$Root,
    [string]$DistDir,
    [string]$BuildProfile,
    [string]$InstallerPath,
    [string]$CachePath
) {
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $CachePath -PathType Leaf)) {
        return $false
    }
    try {
        $cache = Get-Content -LiteralPath $CachePath -Raw | ConvertFrom-Json
        $signature = Get-EngramInstallerSignature $Root $DistDir $BuildProfile
        $installer = Get-Item -LiteralPath $InstallerPath
        return (
            $cache.schema_version -eq 1 -and
            $cache.input_signature -eq $signature -and
            [int64]$cache.output_length -eq $installer.Length -and
            $cache.output_sha256 -eq (Get-EngramSha256 $InstallerPath)
        )
    } catch {
        return $false
    }
}

function Write-EngramInstallerCache(
    [string]$Root,
    [string]$DistDir,
    [string]$BuildProfile,
    [string]$InstallerPath,
    [string]$CachePath
) {
    $installer = Get-Item -LiteralPath $InstallerPath
    $cacheDir = Split-Path -Parent $CachePath
    if (-not (Test-Path -LiteralPath $cacheDir)) {
        New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
    }
    [ordered]@{
        schema_version = 1
        build_profile = $BuildProfile
        input_signature = Get-EngramInstallerSignature $Root $DistDir $BuildProfile
        output_length = [int64]$installer.Length
        output_sha256 = Get-EngramSha256 $InstallerPath
    } | ConvertTo-Json | Set-Content -LiteralPath $CachePath -Encoding UTF8
}
