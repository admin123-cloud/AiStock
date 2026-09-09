param(
  [string]$TaskName = "AiStock QMT Mini Broker Snapshot Sync",
  [string]$At = "09:05",
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunnerScript = Join-Path $RootDir "scripts\run_qmtmini_broker_snapshot_sync.ps1"
if (-not (Test-Path -LiteralPath $RunnerScript)) {
  throw "QMT Mini broker snapshot runner not found: $RunnerScript"
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`" -PythonExe `"$PythonExe`""

$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Args -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $At
$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal `
  -UserId $env:USERNAME `
  -LogonType Interactive `
  -RunLevel Highest

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description "Sync QMT Mini read-only account capital and positions into AiStock G3 broker_state before the trading session. This task does not submit orders." `
  -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: Monday-Friday at $At"
Write-Output "Runner script: $RunnerScript"
Write-Output "PythonExe: $PythonExe"
