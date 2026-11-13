<#
Install the interactive Windows Task Scheduler job that drives the already
configured TongdaXin client after the close.

The task intentionally uses InteractiveToken: TDX renders a native desktop
window and cannot be driven from a non-interactive service session.  No
username/password is stored in the task or in this repository.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AiStock_TDXAutoDownload',
    [string]$RepoRoot = 'F:\Aistock\app',
    [string]$PythonExe = 'F:\python3.10\python.exe',
    [string]$StartTime = '16:05'
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $RepoRoot 'scripts\tdx_postclose_client.py'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "TDX automation script not found: $scriptPath"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument ('"{0}" --tdx-exe "D:\TDX\TdxW.exe" --tdx-root "D:\TDX"' -f $scriptPath) `
    -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At ([datetime]::ParseExact($StartTime, 'HH:mm', $null))
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 40) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'AiStock: 登录通达信并下载盘后日线/5分钟线；不保存或读取登录凭证。' `
    -User $env:USERNAME `
    -RunLevel Limited `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author
