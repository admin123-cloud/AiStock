$ErrorActionPreference = "Stop"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = "F:\Stock\clickhouse_backup"
$dockerExe = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"

New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$logFile = Join-Path $backupRoot "backup_task.log"
if (!(Test-Path $dockerExe)) { throw "docker.exe not found: $dockerExe" }

Write-Output "[backup] start $ts" | Tee-Object -FilePath $logFile -Append

$cmdData = "tar -czf /backup/clickhouse_data_${ts}.tar.gz -C /from ."
& $dockerExe run --rm -v aistock_clickhouse_data:/from -v /run/desktop/mnt/host/f/Stock/clickhouse_backup:/backup alpine sh -c $cmdData | Tee-Object -FilePath $logFile -Append

$cmdLog = "tar -czf /backup/clickhouse_log_${ts}.tar.gz -C /from ."
& $dockerExe run --rm -v aistock_clickhouse_log:/from -v /run/desktop/mnt/host/f/Stock/clickhouse_backup:/backup alpine sh -c $cmdLog | Tee-Object -FilePath $logFile -Append

Get-ChildItem -Path $backupRoot -Filter "clickhouse_*.tar.gz" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 2 |
  Remove-Item -Force -ErrorAction SilentlyContinue

Write-Output "[backup] done" | Tee-Object -FilePath $logFile -Append
