param(
  [string]$PythonExe = "F:\python3.10\pythonw.exe",
  [switch]$EnableNotifications,
  [switch]$WithoutNotifications,
  [switch]$EnableRepairs,
  [switch]$Plan
)
$ErrorActionPreference = 'Stop'
$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ScriptPath = Join-Path $RootDir 'scripts\run_operations_monitor.py'
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python not found: $PythonExe" }
$TaskArguments = '"{0}"' -f $ScriptPath
# Keep installation in read-only mode unless notification takeover is explicit.
# WithoutNotifications is retained for callers of the old installer contract.
$NotificationsEnabled = $EnableNotifications -and (-not $WithoutNotifications)
if ($NotificationsEnabled) { $TaskArguments += ' --notify' }
if ($EnableRepairs) { $TaskArguments += ' --repair' }
if ($Plan) {
  [pscustomobject]@{ Execute=$PythonExe; Arguments=$TaskArguments; WorkingDirectory=$RootDir; NotificationsEnabled=$NotificationsEnabled; BaselinesExistingIncidents=$NotificationsEnabled; RepairsEnabled=[bool]$EnableRepairs } | ConvertTo-Json
  exit 0
}
$CliPython = Join-Path (Split-Path -Parent $PythonExe) 'python.exe'
if (-not (Test-Path -LiteralPath $CliPython)) { $CliPython = $PythonExe }
if ($NotificationsEnabled) {
  & $CliPython (Join-Path $RootDir 'scripts\publish_operations.py') --baseline-notifications
  if ($LASTEXITCODE -ne 0) { throw 'Could not baseline historic incidents before enabling notifications' }
}
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $TaskArguments -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable
Register-ScheduledTask -TaskName 'AiStock Runtime Health Publisher' -Action $Action -Trigger $Trigger -Settings $Settings -Description 'AiStock read-only health and delivery publisher; notifications require explicit takeover' -Force | Out-Null
