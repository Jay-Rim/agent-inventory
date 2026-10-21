param(
    [string]$Python = 'python',
    [string]$Config = (Join-Path $PSScriptRoot '..\inventory.config.json')
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$configPath = (Resolve-Path -LiteralPath $Config).Path
$configData = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$outputPath = $configData.output
if (-not $outputPath) { $outputPath = '.inventory' }
if (-not [System.IO.Path]::IsPathRooted($outputPath)) {
    $outputPath = Join-Path (Split-Path -Parent $configPath) $outputPath
}
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
$runLog = Join-Path $outputPath 'scheduled-scan.log'
Push-Location (Join-Path $PSScriptRoot '..')
$scanExit = 1
try {
    # Capture the latest invocation; detailed collection history remains in SQLite.
    $ErrorActionPreference = 'Continue'
    & $Python -m agent_inventory --config $configPath scan *> $runLog
    $scanExit = $LASTEXITCODE
    if ($null -eq $scanExit) { $scanExit = 1 }
} finally { Pop-Location }
exit $scanExit
