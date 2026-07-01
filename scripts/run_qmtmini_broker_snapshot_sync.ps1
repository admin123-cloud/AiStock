param(
  [string]$PythonExe = "python",
  [string]$LogFile = "F:\Stock\AiStockData\data\runtime\gen3_state_alpha\qmtmini_broker_snapshot_sync.log"
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$SyncScript = Join-Path $RootDir "scripts\sync_qmtmini_broker_snapshot.py"
if (-not (Test-Path -LiteralPath $SyncScript)) {
  throw "QMT Mini broker snapshot sync script not found: $SyncScript"
}

$LogDir = Split-Path -Parent $LogFile
if ($LogDir -and -not (Test-Path -LiteralPath $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$Timestamp] START qmtmini broker snapshot sync" | Out-File -LiteralPath $LogFile -Append -Encoding utf8

Push-Location $RootDir
try {
  & $PythonExe $SyncScript --log-file $LogFile
  $ExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}

$FinishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$FinishedAt] END qmtmini broker snapshot sync exit_code=$ExitCode" | Out-File -LiteralPath $LogFile -Append -Encoding utf8

exit $ExitCode
