param(
  [string]$TaskName = "AiStock QMT xtquant Unified Collector",
  [string]$PythonExe = "F:\python3.10\python.exe",
  [string]$At = "09:35",
  [int]$RepeatMinutes = 5,
  [string]$ActiveStart = "09:30",
  [string]$ActiveEnd = "15:10",
  [string]$Periods = "5m,15m,30m,60m",
  [string]$Universe = "index"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunnerScript = Join-Path $RootDir "scripts\run_qmt_xtquant_collector.ps1"
if (-not (Test-Path -LiteralPath $RunnerScript)) {
  throw "QMT xtquant collector runner not found: $RunnerScript"
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`" -PythonExe `"$PythonExe`" -Scenario intraday -Periods `"$Periods`" -Universe `"$Universe`" -ActiveStart `"$ActiveStart`" -ActiveEnd `"$ActiveEnd`""

$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Args -WorkingDirectory $RootDir
$StartTime = [datetime]::ParseExact($At, "HH:mm", $null)
$StartAt = (Get-Date).Date.AddHours($StartTime.Hour).AddMinutes($StartTime.Minute)
if ($StartAt -lt (Get-Date)) {
  $StartAt = $StartAt.AddDays(1)
}
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt `
  -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
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
  -Description "Run AiStock QMT/xtquant unified collector on the Windows host. This task writes market data only and does not submit orders." `
  -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: starts at $At, repeats every $RepeatMinutes minutes"
Write-Output "Active window: weekdays $ActiveStart~$ActiveEnd"
Write-Output "Runner script: $RunnerScript"
Write-Output "PythonExe: $PythonExe"
Write-Output "Periods: $Periods"
Write-Output "Universe: $Universe"
