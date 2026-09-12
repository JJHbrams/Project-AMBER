#Requires -Version 5
<# Dedicated bubble build. Does not stop or deploy any running application. #>
param([switch]$DebugBuild)
$ErrorActionPreference = 'Stop'
$NativeRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'native-bubble-shell'
Push-Location -LiteralPath $NativeRoot
try {
    $buildArgs = @('build', '--locked', '--offline')
    if (-not $DebugBuild) { $buildArgs += '--release' }
    & cargo @buildArgs
    if ($LASTEXITCODE -ne 0) { throw 'Native bubble build failed' }
} finally { Pop-Location }
