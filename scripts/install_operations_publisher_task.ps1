param(
  [string]$PythonExe = "F:\python3.10\pythonw.exe",
  [switch]$WithoutNotifications
)
$ErrorActionPreference = 'Stop'
$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ScriptPath = Join-Path $RootDir 'scripts\publish_operations.py'
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python not found: $PythonExe" }
$TaskArguments = '"{0}"' -f $ScriptPath
if (-not $WithoutNotifications) { $TaskArguments += ' --notify' }
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $TaskArguments -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable
Register-ScheduledTask -TaskName 'AiStock Operations Publisher' -Action $Action -Trigger $Trigger -Settings $Settings -Description '发布后台任务清单与数据异常事件；恢复窗口超时后发送合并提醒' -Force | Out-Null
