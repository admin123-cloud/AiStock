param(
  [string]$TaskName = "AiStock Weekly Data Integrity Audit",
  [string]$PythonExe = "F:\python3.10\pythonw.exe",
  [string]$At = "10:30",
  [switch]$Plan
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$ScriptPath = Join-Path $RootDir "scripts\run_weekly_data_audit.py"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Python not found: $PythonExe" }
if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "Weekly audit script not found: $ScriptPath" }
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument ('"{0}" --days 30' -f $ScriptPath) -WorkingDirectory $RootDir
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $At
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable
if ($Plan) {
  [pscustomobject]@{TaskName=$TaskName; Execute=$PythonExe; Arguments=$Action.Arguments; Schedule="每周日 $At"; ReadOnly=$true} | ConvertTo-Json
  exit 0
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "每周只读审计最近30个交易日数据交付；不下载、不修复、不备份" -Force | Out-Null
Write-Output "Installed scheduled task: $TaskName"
