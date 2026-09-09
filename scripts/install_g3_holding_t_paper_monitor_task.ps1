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
$Runner = Join-Path $RepoRoot 'scripts\run_holding_t_service.py'
$TaskCommand = '"{0}" "{1}"' -f $Python, $Runner
& schtasks.exe /Create /TN $TaskName /TR $TaskCommand /SC DAILY /ST 09:30 /RI 1 /DU 05:30 /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "schtasks registration failed with exit code $LASTEXITCODE"
}
$CurrentTask = Get-ScheduledTask -TaskName $TaskName
$ReviewTrigger = New-ScheduledTaskTrigger -Daily -At '16:00'
Set-ScheduledTask -TaskName $TaskName -Trigger @($CurrentTask.Triggers + $ReviewTrigger) | Out-Null
Write-Output "Installed: $TaskName (09:30 every minute + 16:00 review; paper-only)"
