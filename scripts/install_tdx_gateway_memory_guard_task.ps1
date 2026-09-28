param(
  [double]$ThresholdGB = 16,
  [int]$Port = 8765,
  [string]$TaskName = "AiStock TDX Gateway Memory Guard",
  [switch]$EnsureRunning
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$GuardScript = Join-Path $RootDir "scripts\tdx_gateway_memory_guard.ps1"
if (-not (Test-Path -LiteralPath $GuardScript)) {
  throw "Guard script not found: $GuardScript"
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Args = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$GuardScript`"",
  "-ThresholdGB", $ThresholdGB,
  "-Port", $Port
) -join " "
if ($EnsureRunning) {
  $Args = "$Args -EnsureRunning"
}

$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Args -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) `
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
  -Description "Stop AiStock TDX Gateway when private memory exceeds threshold. Does not auto-restart unless installed with -EnsureRunning." `
  -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Guard script: $GuardScript"
Write-Output "ThresholdGB: $ThresholdGB"
Write-Output "EnsureRunning: $EnsureRunning"
