<#
.SYNOPSIS
    Run SSMA for Oracle console against the HR-Pro lab.

.DESCRIPTION
    Validates that variables in ssma_variables.xml have been populated, then
    invokes SSMAforOracleConsole.exe with the project script.

.NOTES
    Windows-only. See conversion/ssma/README.md for installation details.
    Run from a PowerShell prompt; not pwsh on macOS/Linux.
#>
[CmdletBinding()]
param(
    [string]$SsmaExe = "${env:ProgramFiles}\Microsoft SQL Server Migration Assistant for Oracle\Bin\SSMAforOracleConsole.exe"
)

$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConvRoot   = Resolve-Path (Join-Path $ScriptRoot '..\conversion')
$SsmaDir    = Join-Path $ConvRoot 'ssma'
$OutDir     = Join-Path $ConvRoot 'converted\ssma'

if (-not (Test-Path $SsmaExe)) {
    throw "SSMA console not found at: $SsmaExe. Install SSMA for Oracle or pass -SsmaExe."
}

# Refuse to run with unsubstituted placeholders in variables file.
$vars = Get-Content (Join-Path $SsmaDir 'ssma_variables.xml') -Raw
if ($vars -match 'REPLACE_WITH_') {
    throw "ssma_variables.xml still contains REPLACE_WITH_ placeholders. Edit it first."
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Push-Location $SsmaDir
try {
    Write-Host "Running SSMA console..." -ForegroundColor Cyan
    & $SsmaExe -s 'ssma_project.scscript' -v 'ssma_variables.xml'
    if ($LASTEXITCODE -ne 0) {
        throw "SSMA exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

# Copy generated T-SQL into a stable location for 03_conversion_diff.py.
$projectFolder = ([xml]$vars).variables.variable |
    Where-Object { $_.name -eq 'ProjectFolder' } |
    Select-Object -ExpandProperty value
$tsqlSrc = Join-Path $projectFolder 'output'

if (Test-Path $tsqlSrc) {
    Copy-Item -Recurse -Force "$tsqlSrc\*" $OutDir
    Write-Host "Output staged at $OutDir" -ForegroundColor Green
}
else {
    Write-Warning "Expected T-SQL output at $tsqlSrc was not produced; check SSMA logs."
}
