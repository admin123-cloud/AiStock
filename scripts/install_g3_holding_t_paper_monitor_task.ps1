param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AiStock G3 Holding T Paper Monitor'

if ($Remove) {
    & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
    Write-Output "Removed: $TaskName"
    exit 0
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonCommand = Get-Command pythonw -ErrorAction SilentlyContinue
if ($null -eq $PythonCommand) {
    $PythonCommand = Get-Command python -ErrorAction Stop
}
$Python = $PythonCommand.Source
$Runner = Join-Path $RepoRoot 'scripts\run_g3_holding_t_paper_monitor.py'
$TaskCommand = '"{0}" "{1}"' -f $Python, $Runner
& schtasks.exe /Create /TN $TaskName /TR $TaskCommand /SC DAILY /ST 09:30 /RI 1 /DU 05:30 /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "schtasks registration failed with exit code $LASTEXITCODE"
}
Write-Output "Installed: $TaskName (daily 09:30, every 1 minute; paper-only)"
