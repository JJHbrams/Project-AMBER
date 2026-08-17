# Pure candidate resolvers shared by installer environment setup and tests.

function Resolve-InstallerStaticCharacterCandidates {
    param([Parameter(Mandatory)][string]$ProjectRoot, [Parameter(Mandatory)][string]$CharacterName)
    $characterDir = Join-Path $ProjectRoot "resource\character"
    $candidates = @()
    if ([IO.Path]::IsPathRooted($CharacterName)) {
        $candidates += $CharacterName
        $legacyId = [IO.Path]::GetFileNameWithoutExtension($CharacterName)
        if ((Split-Path $CharacterName -Parent) -eq $characterDir -and $legacyId -match '^[A-Za-z0-9_-]+$') {
            $candidates += Join-Path $characterDir "static\$legacyId.png"
            $candidates += Join-Path $characterDir "sets\$legacyId\character.png"
        }
        return $candidates
    }
    $normalized = $CharacterName -replace '\\','/'
    if ($normalized -match '^resource/character/([A-Za-z0-9_-]+)\.png$') {
        $legacyId = $Matches[1]
        $candidates += Join-Path $characterDir "static\$legacyId.png"
        $candidates += Join-Path $characterDir "sets\$legacyId\character.png"
        $candidates += Join-Path $characterDir "$legacyId.png"
    } elseif ($normalized -match '^resource/character/static/([A-Za-z0-9_-]+)\.png$') {
        $candidates += Join-Path $characterDir "static\$($Matches[1]).png"
    } elseif ($normalized -match '^resource/character/sets/([A-Za-z0-9_-]+)/character\.png$') {
        $candidates += Join-Path $characterDir "sets\$($Matches[1])\character.png"
    } elseif ($CharacterName -match '^[A-Za-z0-9_-]+$') {
        $candidates += Join-Path $characterDir "static\$CharacterName.png"
        $candidates += Join-Path $characterDir "sets\$CharacterName\character.png"
        $candidates += Join-Path $characterDir "$CharacterName.png"
    }
    return $candidates
}

function Resolve-InstallerSequenceCharacterDirectories {
    param([Parameter(Mandatory)][string]$ProjectRoot, [Parameter(Mandatory)][string]$CharacterName)
    $characterDir = Join-Path $ProjectRoot "resource\character"
    $directories = @()
    if ([IO.Path]::IsPathRooted($CharacterName)) {
        $directories += $CharacterName
        $legacyId = Split-Path $CharacterName -Leaf
        if ((Split-Path $CharacterName -Parent) -eq $characterDir -and $legacyId -match '^[A-Za-z0-9_-]+$') {
            $directories += Join-Path $characterDir "sequences\$legacyId"
        }
        return $directories
    }
    $normalized = $CharacterName -replace '\\','/'
    if ($normalized -match '^resource/character/([A-Za-z0-9_-]+)$') {
        $legacyId = $Matches[1]
        $directories += Join-Path $characterDir "sequences\$legacyId"
        $directories += Join-Path $characterDir $legacyId
    } elseif ($normalized -match '^resource/character/sequences/([A-Za-z0-9_-]+)$') {
        $directories += Join-Path $characterDir "sequences\$($Matches[1])"
    } elseif ($CharacterName -match '^[A-Za-z0-9_-]+$') {
        $directories += Join-Path $characterDir "sequences\$CharacterName"
        $directories += Join-Path $characterDir $CharacterName
    }
    return $directories
}
