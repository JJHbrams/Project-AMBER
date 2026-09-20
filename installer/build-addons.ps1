<#
.SYNOPSIS
    Addon bundle acquisition — 빌드 타임에 4종 addon 을 조달해 installer/addons/*.zip 으로 만든다.

    external-wheel.ps1 의 sibling-checkout-first 패턴을 따른다: 이웃 체크아웃
    (C:\Users\<user>\vault623\workspace\projects\<name>) 이 있으면 그걸 우선 쓰고,
    없으면 build\addons\<name> 에 clone 한다. 어느 쪽이든 installer\addons.pin 에 박힌
    ref 의 그 시점 커밋에서 화이트리스트 경로만 뽑아 zip 을 만든다.

    조달 실패(비공개 repo 자격 증명 없음 등)는 빌드를 죽이지 않는다 — 그 addon 은
    status=unavailable 로 addons.json 에 남고, 이유가 화면에 출력된다.

.EXAMPLE
    .\build-addons.ps1
#>
param()
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'addons-schema.ps1')
$ProjectRoot = Split-Path $PSScriptRoot
$AddonsOutDir = Join-Path $PSScriptRoot "addons"
$WorkRoot = Join-Path $ProjectRoot "build\addons"
$PinPath = Join-Path $PSScriptRoot "addons.pin"
$SiblingsRoot = Split-Path $ProjectRoot

function Write-Step($Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok($Message) { Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Warn($Message) { Write-Host "  [!] $Message" -ForegroundColor Yellow }
function Write-Err($Message) { Write-Host "  [X] $Message" -ForegroundColor Red }

function Get-EngramAddonPin {
    if (-not (Test-Path -LiteralPath $PinPath -PathType Leaf)) {
        throw "Addon pin file not found: $PinPath"
    }
    $pin = Get-Content -LiteralPath $PinPath -Raw | ConvertFrom-Json -ErrorAction Stop
    if ($pin.schema -ne 2 -or -not $pin.addons) { throw "Unsupported addons.pin schema (expected 2)." }
    foreach ($addon in $pin.addons) {
        if ($addon.id -notmatch '^[a-z0-9][a-z0-9-]*$') { throw "Invalid addon id in pin: $($addon.id)" }
        if ($addon.source -notin @('git', 'local')) { throw "Unknown addon source: $($addon.source)" }
        if (-not $addon.skill_name) { throw "Addon pin entry missing skill_name: $($addon.id)" }
        if ($null -eq $addon.needs_mcp) { throw "Addon pin entry missing needs_mcp: $($addon.id)" }
        if ($addon.needs_mcp -and -not $addon.mcp) { throw "Addon pin entry needs_mcp=true but has no mcp spec: $($addon.id)" }
        if (-not $addon.map -or @($addon.map).Count -eq 0) { throw "Addon pin entry missing map: $($addon.id)" }
        foreach ($entry in $addon.map) {
            if (-not $entry.src -or $null -eq $entry.dest) { throw "Invalid map entry for addon $($addon.id): src/dest required" }
        }
    }
    return $pin
}

function Get-EngramAddonGitEnv {
    return @{ GIT_TERMINAL_PROMPT = '0'; GIT_ASKPASS = 'echo'; GIT_SSH_COMMAND = 'ssh -oBatchMode=yes' }
}

function Invoke-EngramAddonGit {
    param([string[]]$GitArgs, [string]$WorkingDirectory = $null)
    $envBackup = @{}
    $gitEnv = Get-EngramAddonGitEnv
    foreach ($key in $gitEnv.Keys) {
        $envBackup[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $gitEnv[$key])
    }
    try {
        # git writes routine, expected failures (host unreachable, ref not
        # found, ...) to stderr. With the script-wide $ErrorActionPreference
        # = 'Stop', 2>&1 would otherwise turn that stderr text into a
        # terminating exception here -- silently short-circuiting the
        # remote-then-local-fallback logic in Resolve-EngramAddonRemoteCommit
        # before it ever gets to try the next URL or the local fallback.
        # Scoped to this function only; the caller's preference is untouched.
        $ErrorActionPreference = 'Continue'
        $allArgs = @()
        if ($WorkingDirectory) { $allArgs += @('-C', $WorkingDirectory) }
        $allArgs += $GitArgs
        $output = & git @allArgs 2>&1
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($output | ForEach-Object { $_.ToString() }) }
    } finally {
        foreach ($key in $envBackup.Keys) { [Environment]::SetEnvironmentVariable($key, $envBackup[$key]) }
    }
}

function Resolve-EngramAddonRepo {
    # Only decides WHERE the repository's git objects live (sibling checkout,
    # a cached clone from a previous run, or a fresh clone). It never decides
    # which commit is bundled -- that is Resolve-EngramAddonRemoteCommit's job,
    # and it always resolves the ref against the remote (by URL, not by
    # trusting whatever local branch pointer happens to sit in this
    # repository) before falling back to whatever is checked out locally.
    # A sibling checkout's working tree is never touched (no checkout/pull/
    # reset) because the user may be actively working in it.
    param($Addon)
    $sibling = Join-Path $SiblingsRoot $Addon.id
    if (Test-Path -LiteralPath (Join-Path $sibling '.git')) {
        return [pscustomobject]@{ RepoPath = $sibling; SourceUrl = 'sibling-checkout:' + $sibling; Cloned = $false }
    }
    $target = Join-Path $WorkRoot $Addon.id
    $urls = @($Addon.primary)
    if ($Addon.fallback) { $urls += $Addon.fallback }
    if (Test-Path -LiteralPath (Join-Path $target '.git')) {
        return [pscustomobject]@{ RepoPath = $target; SourceUrl = 'cached-clone:' + $target; Cloned = $false }
    }
    New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    foreach ($url in $urls) {
        $result = Invoke-EngramAddonGit -GitArgs @('clone', '--no-checkout', '--quiet', $url, $target)
        if ($result.ExitCode -eq 0) {
            return [pscustomobject]@{ RepoPath = $target; SourceUrl = 'cloned:' + $target; Cloned = $true }
        }
        Write-Warn "clone failed ($url): $((@($result.Output) | Select-Object -Last 1))"
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
    }
    return $null
}

function Resolve-EngramAddonRemoteCommit {
    # Resolves the pin's ref against the REMOTE, by URL, regardless of
    # whether $RepoPath is a sibling checkout, a cached clone, or freshly
    # cloned -- a local branch pointer (e.g. an unpushed/uncommitted
    # "master") is never trusted on its own. Tries each URL in order
    # (primary, then fallback) with `git fetch <url> <ref>`, which does not
    # require a remote named "origin" to exist or be correctly configured.
    # Only when every URL fails to fetch does this fall back to whatever the
    # ref resolves to locally, and it reports that fallback explicitly so a
    # human can see it happened instead of silently shipping local state.
    param([string]$RepoPath, [string]$Ref, [string[]]$Urls)
    $errors = [Collections.Generic.List[string]]::new()
    foreach ($url in $Urls) {
        if (-not $url) { continue }
        $fetch = Invoke-EngramAddonGit -GitArgs @('fetch', '--quiet', $url, $Ref) -WorkingDirectory $RepoPath
        if ($fetch.ExitCode -eq 0) {
            $rev = Invoke-EngramAddonGit -GitArgs @('rev-parse', '--verify', '--quiet', 'FETCH_HEAD^{commit}') -WorkingDirectory $RepoPath
            if ($rev.ExitCode -eq 0 -and $rev.Output.Count -gt 0) {
                return [pscustomobject]@{
                    Commit = $rev.Output[0].Trim(); ResolvedFrom = 'remote'; RemoteUrl = $url
                    Errors = @($errors)
                }
            }
            $errors.Add("fetch OK but ref '$Ref' did not resolve to a commit ($url)")
        } else {
            $errors.Add("fetch failed ($url): $((@($fetch.Output) | Select-Object -Last 1))")
        }
    }
    $local = Invoke-EngramAddonGit -GitArgs @('rev-parse', '--verify', '--quiet', ($Ref + '^{commit}')) -WorkingDirectory $RepoPath
    if ($local.ExitCode -eq 0 -and $local.Output.Count -gt 0) {
        return [pscustomobject]@{
            Commit = $local.Output[0].Trim(); ResolvedFrom = 'local-fallback'; RemoteUrl = $null
            Errors = @($errors)
        }
    }
    $errors.Add("local ref '$Ref' also did not resolve to a commit in $RepoPath")
    return [pscustomobject]@{ Commit = $null; ResolvedFrom = 'failed'; RemoteUrl = $null; Errors = @($errors) }
}

function Resolve-EngramAddonMapDest {
    # Resolves a repo/local-relative $Path to its destination path inside the
    # installed skill, per the pin's src->dest map. This is the single place
    # that turns a repo layout (skills/image-forge/SKILL.md,
    # adapters/claude/SKILL.md, .agents/skills/session-orchestrate/SKILL.md,
    # ...) into the flat skill-root layout Claude Code expects
    # (<skill>/SKILL.md). Returns $null when $Path is not covered by the map
    # (i.e. excluded from the bundle).
    param([string]$Path, [array]$Map)
    foreach ($entry in $Map) {
        $src = [string]$entry.src
        $dest = [string]$entry.dest
        if ($src -eq '*') {
            $destPrefix = $dest.TrimEnd('/')
            if ($destPrefix) { return "$destPrefix/$Path" }
            return $Path
        }
        if ($src.EndsWith('/')) {
            $prefix = $src.TrimEnd('/')
            if ($Path -eq $prefix -or $Path.StartsWith($prefix + '/')) {
                $destPrefix = $dest.TrimEnd('/')
                $rel = $Path.Substring($prefix.Length).TrimStart('/')
                if ($destPrefix -and $rel) { return "$destPrefix/$rel" }
                if ($destPrefix) { return $destPrefix }
                return $rel
            }
        } elseif ($Path -eq $src) {
            return $dest
        }
    }
    return $null
}

function Add-EngramZipEntryBytes {
    # LastWriteTime 를 명시하지 않으면 .NET 이 "지금"을 찍는다 -- 같은 커밋에서
    # 다시 조달해도 zip 바이트가 매번 달라져 installer-cache 서명이 항상 깨진다.
    # 고정 타임스탬프로 재현 가능하게 만든다 (ZIP DOS 시간의 최소값인 1980-01-01).
    param($Archive, [string]$EntryName, [byte[]]$Bytes)
    if (-not $Bytes) { $Bytes = [byte[]]@() }
    $entry = $Archive.CreateEntry($EntryName, [IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    $stream = $entry.Open()
    try { $stream.Write($Bytes, 0, $Bytes.Length) } finally { $stream.Dispose() }
}

function Get-EngramGitBlobBytes {
    param([string]$RepoPath, [string]$BlobHash)
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = "-C `"$RepoPath`" cat-file blob $BlobHash"
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($psi)
    $memory = New-Object IO.MemoryStream
    $process.StandardOutput.BaseStream.CopyTo($memory)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "git cat-file failed for blob $BlobHash" }
    # A comma-wrapped return prevents PowerShell from unrolling a zero-length
    # array (an empty tracked file, e.g. an __init__.py) into $null.
    return ,$memory.ToArray()
}

function New-EngramGitAddonZip {
    param([string]$RepoPath, [string]$Commit, [array]$Map, [string]$ZipPath)
    $listing = Invoke-EngramAddonGit -GitArgs @('ls-tree', '-r', $Commit) -WorkingDirectory $RepoPath
    if ($listing.ExitCode -ne 0) { throw "git ls-tree failed for $Commit" }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path $ZipPath) | Out-Null
    $entries = [Collections.Generic.List[string]]::new()
    $stream = [IO.File]::Open($ZipPath, [IO.FileMode]::CreateNew)
    try {
        $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($line in $listing.Output) {
                if (-not $line) { continue }
                $tabIndex = $line.IndexOf("`t")
                if ($tabIndex -lt 0) { continue }
                $meta = $line.Substring(0, $tabIndex).Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
                $path = $line.Substring($tabIndex + 1)
                if ($meta.Count -lt 3 -or $meta[1] -ne 'blob') { continue }
                $dest = Resolve-EngramAddonMapDest -Path $path -Map $Map
                if (-not $dest) { continue }
                $bytes = Get-EngramGitBlobBytes -RepoPath $RepoPath -BlobHash $meta[2]
                Add-EngramZipEntryBytes -Archive $archive -EntryName $dest.Replace('\', '/') -Bytes $bytes
                $entries.Add($dest)
            }
        } finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
    return [pscustomobject]@{ Count = $entries.Count; Entries = @($entries | Sort-Object) }
}

function New-EngramLocalAddonZip {
    param([string]$SourceDir, [array]$Map, [string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path $ZipPath) | Out-Null
    $base = [IO.Path]::GetFullPath($SourceDir).TrimEnd('\')
    $entries = [Collections.Generic.List[string]]::new()
    $stream = [IO.File]::Open($ZipPath, [IO.FileMode]::CreateNew)
    try {
        $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($file in Get-ChildItem -LiteralPath $base -Recurse -File) {
                $relative = $file.FullName.Substring($base.Length + 1).Replace('\', '/')
                $dest = Resolve-EngramAddonMapDest -Path $relative -Map $Map
                if (-not $dest) { continue }
                $bytes = [IO.File]::ReadAllBytes($file.FullName)
                Add-EngramZipEntryBytes -Archive $archive -EntryName $dest -Bytes $bytes
                $entries.Add($dest)
            }
        } finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
    return [pscustomobject]@{ Count = $entries.Count; Entries = @($entries | Sort-Object) }
}

function Add-EngramAddonMetaToZip {
    # Bakes the addon's identity (id, skill_name, needs_mcp, mcp spec) into the
    # zip itself as ".addon-meta.json" at the archive root. This is the single
    # source Install-EngramAddons and Register-EngramAddonMcp read at install
    # time -- metadata can never separate from the payload it describes,
    # because it travels inside the same zip that Inno Setup stages into
    # {tmp}\addon-bundle (which never carries a sibling addons.json).
    param([Parameter(Mandatory)][string]$ZipPath, [Parameter(Mandatory)]$Addon)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $meta = [ordered]@{
        id         = $Addon.id
        skill_name = $Addon.skill_name
        needs_mcp  = [bool]$Addon.needs_mcp
    }
    if ($Addon.needs_mcp) { $meta.mcp = $Addon.mcp }
    $json = ($meta | ConvertTo-Json -Depth 6)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $stream = [IO.File]::Open($ZipPath, [IO.FileMode]::Open)
    try {
        $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Update)
        try {
            $existing = $archive.GetEntry('.addon-meta.json')
            if ($existing) { $existing.Delete() }
            Add-EngramZipEntryBytes -Archive $archive -EntryName '.addon-meta.json' -Bytes $bytes
        } finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
}

function Get-EngramFileSha256 {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}

Write-Step "loading pin"
$pin = Get-EngramAddonPin
Write-Ok "$($pin.addons.Count) addons defined"

if (Test-Path -LiteralPath $AddonsOutDir) {
    Get-ChildItem -LiteralPath $AddonsOutDir -Filter '*.zip' -File | Remove-Item -Force
} else {
    New-Item -ItemType Directory -Force -Path $AddonsOutDir | Out-Null
}

$results = [Collections.Generic.List[object]]::new()
foreach ($addon in $pin.addons) {
    Write-Step "addon: $($addon.id)"
    $zipPath = Join-Path $AddonsOutDir ($addon.id + '.zip')
    try {
        if ($addon.source -eq 'local') {
            $localDir = Join-Path $ProjectRoot $addon.local_path
            if (-not (Test-Path -LiteralPath $localDir -PathType Container)) {
                throw "Local addon path not found: $localDir"
            }
            $commitResult = Invoke-EngramAddonGit -GitArgs @('log', '-1', '--format=%H', '--', $addon.local_path) -WorkingDirectory $ProjectRoot
            $commit = if ($commitResult.ExitCode -eq 0 -and $commitResult.Output.Count -gt 0 -and $commitResult.Output[0]) {
                $commitResult.Output[0].Trim()
            } else {
                $headResult = Invoke-EngramAddonGit -GitArgs @('rev-parse', 'HEAD') -WorkingDirectory $ProjectRoot
                if ($headResult.ExitCode -eq 0) { $headResult.Output[0].Trim() } else { 'uncommitted' }
            }
            $zipResult = New-EngramLocalAddonZip -SourceDir $localDir -Map $addon.map -ZipPath $zipPath
            $source = 'repo-internal:' + $addon.local_path
            $resolvedFrom = 'internal'
            $remoteUrl = $null
            $fetchErrors = @()
        } else {
            $repo = Resolve-EngramAddonRepo -Addon $addon
            if (-not $repo) { throw "Could not acquire repository (missing credentials or network failure): $($addon.primary)" }
            $urls = @($addon.primary)
            if ($addon.fallback) { $urls += $addon.fallback }
            $resolved = Resolve-EngramAddonRemoteCommit -RepoPath $repo.RepoPath -Ref $addon.ref -Urls $urls
            if (-not $resolved.Commit) {
                throw "Could not resolve ref '$($addon.ref)' against the remote or locally (tried: $($urls -join ', ')): $(($resolved.Errors) -join '; ')"
            }
            if ($resolved.ResolvedFrom -eq 'local-fallback') {
                Write-Warn "$($addon.id): 원격에서 '$($addon.ref)' 를 확인하지 못해 로컬 체크아웃 상태를 그대로 번들에 담았습니다 (커밋되지 않았거나 푸시되지 않은 변경이 포함될 수 있음). 시도한 원격: $($urls -join ', ') / 오류: $(($resolved.Errors) -join '; ')"
            }
            $commit = $resolved.Commit
            $resolvedFrom = $resolved.ResolvedFrom
            $remoteUrl = $resolved.RemoteUrl
            $fetchErrors = @($resolved.Errors)
            $zipResult = New-EngramGitAddonZip -RepoPath $repo.RepoPath -Commit $commit -Map $addon.map -ZipPath $zipPath
            if ($zipResult.Count -eq 0) { throw "No files matched the map: $(($addon.map | ForEach-Object { $_.src }) -join ', ')" }
            $source = $repo.SourceUrl
        }
        Add-EngramAddonMetaToZip -ZipPath $zipPath -Addon $addon
        $count = $zipResult.Count
        $bytes = (Get-Item -LiteralPath $zipPath).Length
        $sha256 = Get-EngramFileSha256 $zipPath
        $results.Add([pscustomobject]@{
            name = $addon.id; source = $source; commit = $commit; ref = $addon.ref
            sha256 = $sha256; bytes = $bytes; files = $count; status = 'available'
            skill_name = $addon.skill_name; entries = $zipResult.Entries
            resolved_from = $resolvedFrom; remote_url = $remoteUrl; fetch_errors = $fetchErrors
        })
        $resolvedNote = if ($resolvedFrom -eq 'local-fallback') { " resolved_from=local-fallback(!)" } else { " resolved_from=$resolvedFrom" }
        Write-Ok "$($addon.id): commit=$($commit.Substring(0, [Math]::Min(12,$commit.Length))) files=$count bytes=$bytes source=$source$resolvedNote"
    } catch {
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue }
        $reason = $_.Exception.Message
        $results.Add([pscustomobject]@{
            name = $addon.id; source = $null; commit = $null; ref = $addon.ref
            sha256 = $null; bytes = 0; files = 0; status = 'unavailable'; reason = $reason
            skill_name = $addon.skill_name; entries = @()
            resolved_from = $null; remote_url = $null; fetch_errors = @()
        })
        Write-Warn "$($addon.id): acquisition failed - $reason"
    }
}

$manifest = [pscustomobject]@{
    schema = $EngramAddonManifestSchema
    generated_at = (Get-Date).ToUniversalTime().ToString('o')
    addons = $results
}
$manifestPath = Join-Path $AddonsOutDir 'addons.json'
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))

$available = @($results | Where-Object { $_.status -eq 'available' })
$unavailable = @($results | Where-Object { $_.status -eq 'unavailable' })
$totalBytes = ($available | Measure-Object -Property bytes -Sum).Sum
if (-not $totalBytes) { $totalBytes = 0 }

$localFallback = @($available | Where-Object { $_.resolved_from -eq 'local-fallback' })

Write-Step "summary"
Write-Ok "available addons: $($available.Count)/$($pin.addons.Count)"
if ($unavailable.Count) {
    foreach ($item in $unavailable) { Write-Warn "unavailable: $($item.name) - $($item.reason)" }
}
if ($localFallback.Count) {
    foreach ($item in $localFallback) {
        Write-Warn "$($item.name): 원격 확인에 실패해 로컬 체크아웃 상태로 번들에 담겼습니다 (commit=$($item.commit)). 배포 전 이 addon 의 원격 접근 가능 여부를 확인하세요."
    }
}
Write-Ok "installer/addons total size: $([Math]::Round($totalBytes / 1MB, 3)) MB ($totalBytes bytes)"
Write-Ok "manifest: $manifestPath"
