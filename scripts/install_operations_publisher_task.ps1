param(
  [string]$PythonExe = "F:\python3.10\pythonw.exe",
  [switch]$WithoutNotifications,
  [switch]$EnableRepairs,
  [switch]$Plan
)
$ErrorActionPreference = 'Stop'
$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ScriptPath = Join-Path $RootDir 'scripts\run_operations_monitor.py'
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python not found: $PythonExe" }
$TaskArguments = '"{0}"' -f $ScriptPath
if (-not $WithoutNotifications) { $TaskArguments += ' --notify' }
if ($EnableRepairs) { $TaskArguments += ' --repair' }
if ($Plan) {
  [pscustomobject]@{ Execute=$PythonExe; Arguments=$TaskArguments; WorkingDirectory=$RootDir; NotificationsEnabled=(-not $WithoutNotifications); RepairsEnabled=[bool]$EnableRepairs } | ConvertTo-Json
  exit 0
}
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $TaskArguments -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable
Register-ScheduledTask -TaskName 'AiStock Runtime Health Publisher' -Action $Action -Trigger $Trigger -Settings $Settings -Description '统一运行监控：先发布健康快照，再验收交付与记录异常；恢复窗口超时后合并提醒' -Force | Out-Null
