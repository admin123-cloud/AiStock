param(
  [string]$TaskName = "AiStock Runtime Health Publisher",
  # This publisher has no interactive output.  pythonw prevents Windows from
  # creating a console window for every five-minute health snapshot run.
  [string]$PythonExe = "F:\python3.10\pythonw.exe",
  [string]$At = "08:45",
  [int]$RepeatMinutes = 5,
  [int]$DurationHours = 11
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$ScriptPath = Join-Path $RootDir "scripts\publish_runtime_health.py"
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python not found: $PythonExe" }
if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "Runtime health publisher not found: $ScriptPath" }
if ($RepeatMinutes -lt 1) { throw "RepeatMinutes must be positive" }
if ($DurationHours -lt 1) { throw "DurationHours must be positive" }

$Duration = ([TimeSpan]::FromHours($DurationHours)).ToString("hh':'mm")
$TaskRun = '"{0}" "{1}"' -f $PythonExe, $ScriptPath
& schtasks.exe /Create /TN $TaskName /TR $TaskRun /SC DAILY /ST $At /RI $RepeatMinutes /DU $Duration /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks.exe failed with exit code $LASTEXITCODE" }
$Settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
  -MultipleInstances IgnoreNew `
  -RestartCount 2 `
  -RestartInterval (New-TimeSpan -Minutes 5) `
  -StartWhenAvailable
Set-ScheduledTask -TaskName $TaskName -Settings $Settings | Out-Null
Write-Output "Installed scheduled task: $TaskName (execution limit: 2 minutes)"
