param(
    [ValidateSet('Inspect','Backup','Verify','Retention-Plan','Prune','Reconcile')][string]$Mode = 'Inspect',
    [string]$PythonExe = 'F:\python3.10\python.exe',
    [string]$BackupRoot = 'F:\Stock\clickhouse_backup\native',
    [string]$Archive = '',
    [string]$BackupId = '',
    [switch]$Full,
    [switch]$Scheduled
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskArgs = @((Join-Path $PSScriptRoot 'manage_clickhouse_backup.py'), '--mode', $Mode.ToLowerInvariant(), '--root', $BackupRoot)
if ($Full) { $taskArgs += '--full' }
if ($Scheduled) { $taskArgs += '--scheduled' }
if ($Archive) { $taskArgs += @('--archive', $Archive) }
if ($BackupId) { $taskArgs += @('--backup-id', $BackupId) }
Push-Location $taskRoot
try {
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    & $PythonExe @taskArgs
    if ($LASTEXITCODE -ne 0) { throw "Native backup command failed, exit=$LASTEXITCODE; preserve all prior batches" }
} finally { Pop-Location }
