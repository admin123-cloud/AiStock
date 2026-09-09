param(
  [ValidateSet('Plan','Apply','Activate','Verify','Rollback')][string]$Mode = 'Plan',
  [string]$BackupDir,
  [string]$SourceRoot = 'F:\Stock\AiStock-core',
  [string]$TargetRoot = (Split-Path $PSScriptRoot -Parent),
  [string]$PythonExe = 'F:\python3.10\python.exe',
  [string]$ApiBase = 'http://127.0.0.1:8000'
)
$ErrorActionPreference = 'Stop'
$taskNames = @('AiStock QMT xtquant Intraday Collector', 'AiStock QMT xtquant After Close Repair',
  'AiStock QMT xtquant Daily Coverage Repair', 'AiStock QMT xtquant Night Rolling Repair',
  'AiStock G3 Holding T Paper Monitor')
$TargetRoot = (Resolve-Path -LiteralPath $TargetRoot).Path
$taskVersion = (& git -C $TargetRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine target version' }
function Assert-NoWorkers {
  $activeTasks = @($taskNames | ForEach-Object { Get-ScheduledTask -TaskName $_ } | Where-Object { $_.State -eq 'Running' })
  if ($activeTasks.Count) { throw 'A collector task is running; wait for its safe completion before migration.' }
  $workers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match '(?i)(qmt_fullpush_intraday_aggregator|qmt_xtquant_data_source_task|qmt_xtquant_minute_gap_audit_repair|qmtmini_daily_backfill_validate|qmt_xtquant_minute_backfill_validate|daily_kline_coverage_maintenance|run_ingestion_backlog|run_reference_maintenance|repair_index_daily|repair_sector_daily|run_holding_t_service|run_g3_holding_t_paper_monitor|run_g3_holding_t_daily_review)\.py'
  })
  if ($workers.Count) { throw "Collector workers still active (PIDs $($workers.ProcessId -join ',')); no process is killed by this script." }
}
function Assert-ApiOwner {
  $proof = Invoke-RestMethod -Uri "$ApiBase/api/system/ingestion-owners" -TimeoutSec 10
  $versionMatches = $proof.version -match '^[a-f0-9]{7,40}$' -and $taskVersion.StartsWith([string]$proof.version)
  if (-not $versionMatches -or $proof.legacy_sector_history_enabled -ne $false -or
      $proof.legacy_sector_history_running -ne $false -or $proof.sector_daily_owner -ne 'host') {
    throw 'API does not prove the expected version and disabled/idle legacy sector owner.'
  }
}
if ($Mode -eq 'Plan') {
  if (-not $BackupDir) { $BackupDir = Join-Path 'F:\Stock\AiStockData\artifacts\ingestion-owner-migration' (Get-Date -Format 'yyyyMMdd-HHmmss') }
  if (Test-Path -LiteralPath $BackupDir) { throw 'Use a fresh backup directory; originals are immutable.' }
  New-Item -ItemType Directory -Path $BackupDir | Out-Null
  $inventory = @($taskNames | ForEach-Object {
    $task = Get-ScheduledTask -TaskName $_
    $file = ($_.Replace(' ', '_'))+'.xml'
    [IO.File]::WriteAllText((Join-Path $BackupDir $file), (Export-ScheduledTask -TaskName $_), [Text.Encoding]::Unicode)
    @{name=$_; file=$file; enabled=[bool]$task.Settings.Enabled; state=[string]$task.State}
  })
  [IO.File]::WriteAllText((Join-Path $BackupDir 'inventory.json'), ($inventory | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
  & $PythonExe (Join-Path $PSScriptRoot 'plan_ingestion_owner_migration.py') $BackupDir --source $SourceRoot --target $TargetRoot --version $taskVersion
  if ($LASTEXITCODE -ne 0) { throw 'Migration plan validation failed; tasks unchanged.' }
  Write-Output "Plan only; no tasks changed: $BackupDir"
  exit 0
}
if (-not $BackupDir) { throw 'BackupDir is required' }
$plan = Get-Content -LiteralPath (Join-Path $BackupDir 'plan.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($plan.target -ne $TargetRoot -or ($Mode -ne 'Rollback' -and $plan.version -ne $taskVersion)) { throw 'Plan target/version differs from checkout; regenerate plan.' }
if ($plan.tasks.Count -ne $taskNames.Count -or @($plan.tasks.name | Select-Object -Unique).Count -ne $taskNames.Count -or
    @($plan.tasks | Where-Object { $_.name -notin $taskNames }).Count -gt 0) {
  throw 'Plan must contain exactly the five known ingestion and Holding T task names.'
}
foreach ($row in $plan.tasks) {
  foreach ($pair in @(@($row.file,$row.sha256), @($row.planned_file,$row.planned_sha256))) {
    if ([IO.Path]::GetFileName($pair[0]) -ne $pair[0]) { throw 'Backup entries must be local filenames.' }
    if ((Get-FileHash -LiteralPath (Join-Path $BackupDir $pair[0])).Hash.ToLowerInvariant() -ne $pair[1]) { throw 'Backup or planned XML hash changed.' }
  }
}
function Restore-Originals {
  foreach ($row in $plan.tasks) {
    $xml = [IO.File]::ReadAllText((Join-Path $BackupDir $row.file), [Text.Encoding]::Unicode)
    Register-ScheduledTask -TaskName $row.name -Xml $xml -Force | Out-Null
  }
}
function Assert-PlannedDefinitions {
  foreach ($row in $plan.tasks) {
    [xml]$current = Export-ScheduledTask -TaskName $row.name
    [xml]$expected = [IO.File]::ReadAllText((Join-Path $BackupDir $row.planned_file), [Text.Encoding]::Unicode)
    foreach ($section in @('Actions','Triggers','Principals')) {
      if ($current.Task.$section.InnerXml -ne $expected.Task.$section.InnerXml) { throw "Planned $section differs for $($row.name)" }
    }
  }
}
if ($Mode -eq 'Verify') { Assert-ApiOwner; Assert-PlannedDefinitions; Write-Output 'API ownership, actions and all triggers verified.'; exit 0 }
Assert-NoWorkers
if ($Mode -eq 'Rollback') { Restore-Originals; Write-Output 'Original task XML restored; API rollback remains separately coordinated.'; exit 0 }
Assert-ApiOwner
if ($Mode -eq 'Activate') {
  Assert-PlannedDefinitions
  foreach ($row in $plan.tasks) { if ($row.enabled) { Enable-ScheduledTask -TaskName $row.name | Out-Null } }
  Write-Output 'Existing enabled owners reactivated; no immediate Start-ScheduledTask issued.'
  exit 0
}
foreach ($row in $plan.tasks) {
  $original = [IO.File]::ReadAllText((Join-Path $BackupDir $row.file), [Text.Encoding]::Unicode)
  if ((Export-ScheduledTask -TaskName $row.name) -ne $original) { throw "Task changed after plan: $($row.name)" }
}
try {
  foreach ($row in $plan.tasks) { Disable-ScheduledTask -TaskName $row.name | Out-Null }
  Assert-NoWorkers
  foreach ($row in $plan.tasks) {
    $xml = [IO.File]::ReadAllText((Join-Path $BackupDir $row.planned_file), [Text.Encoding]::Unicode)
    Register-ScheduledTask -TaskName $row.name -Xml $xml -Force | Out-Null
  }
  Assert-PlannedDefinitions
} catch { $failure = $_; Restore-Originals; throw $failure }
Write-Output 'New task definitions staged disabled; use Activate only after owner/data acceptance.'
