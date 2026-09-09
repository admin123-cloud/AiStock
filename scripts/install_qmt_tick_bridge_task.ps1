[CmdletBinding()]
param(
    [string]$TaskName = "AiStock QMT Read-Only Tick Bridge",
    [string]$PythonExe = "F:\python3.10\pythonw.exe",
    [string]$BridgeScript = "F:\Stock\AiStock-core\scripts\qmt_tick_bridge.py"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "pythonw.exe not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $BridgeScript -PathType Leaf)) {
    throw "Tick bridge script not found: $BridgeScript"
}

# QMT Mini is attached to the signed-in desktop session, so this is deliberately
# an interactive-user task rather than a machine service. pythonw.exe keeps the
# read-only bridge windowless while Task Scheduler gives it a durable owner.
$userId = "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument ('"{0}"' -f $BridgeScript) -WorkingDirectory (Split-Path -Parent $BridgeScript)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
