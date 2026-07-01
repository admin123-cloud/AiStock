param(
  [double]$ThresholdGB = 16,
  [int]$Port = 8765,
  [int]$IntervalSeconds = 30,
  [int]$RestartCooldownSeconds = 90,
  [string]$TaskName = "AiStock TDX Gateway Memory Watchdog"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$WatchdogScript = Join-Path $RootDir "scripts\tdx_gateway_memory_watchdog.ps1"
$HiddenLauncher = Join-Path $RootDir "scripts\start_tdx_gateway_memory_watchdog_hidden.vbs"
if (-not (Test-Path -LiteralPath $WatchdogScript)) {
  throw "Watchdog script not found: $WatchdogScript"
}
if (-not (Test-Path -LiteralPath $HiddenLauncher)) {
  throw "Hidden launcher not found: $HiddenLauncher"
}

$WScript = Join-Path $env:SystemRoot "System32\wscript.exe"
$Args = "`"$HiddenLauncher`""

$Action = New-ScheduledTaskAction -Execute $WScript -Argument $Args -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
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
  -Description "Continuously sample AiStock TDX Gateway memory, stop it when private memory exceeds threshold, and restart it after a cooldown." `
  -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Output "Installed and started scheduled task: $TaskName"
Write-Output "Watchdog script: $WatchdogScript"
Write-Output "ThresholdGB: $ThresholdGB"
Write-Output "IntervalSeconds: $IntervalSeconds"
Write-Output "RestartCooldownSeconds: $RestartCooldownSeconds"
