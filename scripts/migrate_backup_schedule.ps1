param(
  [ValidateSet('Plan','Apply','Activate','Verify','Rollback')][string]$Mode = 'Plan',
  [string]$BackupDir,
  [string]$SourceRoot = 'F:\Stock\AiStock',
  [string]$TargetRoot = (Split-Path $PSScriptRoot -Parent),
  [string]$PythonExe = 'F:\python3.10\python.exe',
  [string]$RestoreManifest,
  [switch]$BackupWindowCoordinated
)
$ErrorActionPreference = 'Stop'
$taskName = 'AiStock_ClickHouse_Backup_3AM'
$taskPath = '\'
$TargetRoot = (Resolve-Path -LiteralPath $TargetRoot).Path
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
$version = (& git -C $TargetRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine target checkout version' }
foreach ($value in @($TargetRoot,$SourceRoot,$PythonExe)) {
  if ($value -match '["\r\n]') { throw 'Task paths cannot contain quotes or newlines' }
}
$targetScript = Join-Path $TargetRoot 'scripts\backup_clickhouse_volume_to_fstock.ps1'
$sourceScript = Join-Path $SourceRoot 'scripts\backup_clickhouse_volume_to_fstock.ps1'
$implementation = @('scripts\backup_clickhouse_volume_to_fstock.ps1','scripts\manage_clickhouse_backup.py','services\operations\backup.py')
function Get-Definition { return (Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath) }
function Save-Json($path,$value) {
  [IO.File]::WriteAllText($path,($value | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
}
function Hash-File($path) { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Assert-NoWorkers {
  $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath
  if ($task.State -eq 'Running') { throw 'Backup task is running; wait for its safe completion.' }
  $workers = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match '(?i)(backup_clickhouse_volume_to_fstock\.ps1|manage_clickhouse_backup\.py|verify_clickhouse_backup\.py|qmt_xtquant_data_source_task\.py|qmt_xtquant_minute_gap_audit_repair\.py|rebuild_derived_history\.py|daily_kline_coverage_maintenance\.py)'
  })
  if ($workers.Count) { throw "Backup/repair workers are active (PIDs $($workers.ProcessId -join ',')); no worker is stopped by this installer." }
}
function Assert-BackupReady {
  if (-not $RestoreManifest -or -not (Test-Path -LiteralPath $RestoreManifest -PathType Leaf)) { throw 'RestoreManifest from an actual isolated restore drill is required.' }
  $readOnlyProbe = @'
import json,sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0,sys.argv[1])
from utils.market_warehouse import clickhouse_client
manifest=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
checks=manifest.get('restore_checks') or []
if manifest.get('state')!='archive_verified' or not manifest.get('restore_verified_at'): raise RuntimeError('No completed isolated restore evidence')
if manifest.get('database')!='stock': raise RuntimeError('Activation requires the real stock database restore, not a synthetic probe')
if datetime.fromisoformat(manifest['restore_verified_at']).tzinfo is None: raise RuntimeError('Restore proof needs business timezone')
if not 0 <= (datetime.now().astimezone()-datetime.fromisoformat(manifest['restore_verified_at'])).total_seconds() <= 7*86400: raise RuntimeError('Restore evidence is expired or in the future')
if not checks or not all(row.get('check')=='passed' for row in checks): raise RuntimeError('Restore table checks incomplete')
client=clickhouse_client()
if client.query("SELECT name,path FROM system.disks WHERE name='backups'").result_rows!=[('backups','/backups/')]: raise RuntimeError('Native /backups/ disk unavailable')
active=client.query("SELECT count() FROM system.backups WHERE status NOT IN ('BACKUP_CREATED','BACKUP_FAILED','RESTORED','RESTORE_FAILED')").result_rows[0][0]
if active: raise RuntimeError('An asynchronous backup/restore is still active; parent exit is not completion')
print('Native disk and isolated restore evidence verified; no active server backup/restore.')
'@
  $readOnlyProbe | & $PythonExe -X utf8 - $TargetRoot $RestoreManifest
  if ($LASTEXITCODE -ne 0) { throw 'Native backup activation preflight failed; schedule remains disabled.' }
}
function Set-TextNode($document,$parent,$name,$value) {
  $node = $parent.SelectSingleNode("*[local-name()='$name']")
  if (-not $node) { $node=$document.CreateElement($name,$document.DocumentElement.NamespaceURI); [void]$parent.AppendChild($node) }
  $node.InnerText = $value
}
function Same-DefinitionSections($actual,$expected) {
  foreach ($section in @('Actions','Triggers','Principals')) {
    if ($actual.Task.$section.InnerXml -ne $expected.Task.$section.InnerXml) { return $false }
  }
  return $true
}
if ($Mode -eq 'Plan') {
  if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'Python executable missing' }
  $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath
  $original = Get-Definition
  [xml]$document = $original
  $actions = @($document.Task.Actions.Exec)
  if ($actions.Count -ne 1 -or $document.Task.Actions.ChildNodes.Count -ne 1 -or
      $actions[0].Arguments -notlike ('*"'+$sourceScript+'"*')) {
    throw 'Original action differs from the expected legacy backup script; review before planning.'
  }
  if ($document.Task.Principals.Principal.LogonType -notin @('InteractiveToken','ServiceAccount','S4U')) {
    throw 'Task principal requires credentials not retained in XML; do not overwrite it.'
  }
  $files = @($implementation | ForEach-Object { @{file=$_;sha256=(Hash-File (Join-Path $TargetRoot $_))} })
  $originalTrigger = $document.Task.Triggers.OuterXml
  $originalPrincipal = $document.Task.Principals.OuterXml
  $nextRun = (Get-Date).Date.AddDays(1).AddHours(7)
  $boundary = $nextRun.ToString('yyyy-MM-ddTHH:mm:sszzz')
  $namespace = $document.DocumentElement.NamespaceURI
  $document.Task.Triggers.InnerXml = "<CalendarTrigger xmlns=`"$namespace`"><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>"
  Set-TextNode $document $document.Task.Actions.Exec 'Arguments' "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$targetScript`" -Mode Backup -Scheduled -PythonExe `"$PythonExe`""
  Set-TextNode $document $document.Task.Actions.Exec 'WorkingDirectory' $TargetRoot
  Set-TextNode $document $document.Task.Settings 'Enabled' 'false'
  Set-TextNode $document $document.Task.Settings 'MultipleInstancesPolicy' 'IgnoreNew'
  if (-not $BackupDir) { $BackupDir=Join-Path 'F:\Stock\AiStockData\artifacts\backup-schedule-migration' (Get-Date -Format 'yyyyMMdd-HHmmss') }
  $BackupDir = [IO.Path]::GetFullPath($BackupDir)
  if (Test-Path -LiteralPath $BackupDir) { throw 'Use a fresh evidence directory; originals are immutable.' }
  New-Item -ItemType Directory -Path $BackupDir | Out-Null
  [IO.File]::WriteAllText((Join-Path $BackupDir 'original.xml'),$original,[Text.Encoding]::Unicode)
  [IO.File]::WriteAllText((Join-Path $BackupDir 'planned.xml'),$document.OuterXml,[Text.Encoding]::Unicode)
  $plan = @{schema_version=1;name=$taskName;task_path=$taskPath;source=$SourceRoot;target=$TargetRoot;version=$version;
    original_file='original.xml';original_sha256=(Hash-File (Join-Path $BackupDir 'original.xml'));
    planned_file='planned.xml';planned_sha256=(Hash-File (Join-Path $BackupDir 'planned.xml'));
    enabled=[bool]$task.Settings.Enabled;original_state=[string]$task.State;original_triggers=$originalTrigger;
    original_principal=$originalPrincipal;planned_triggers=$document.Task.Triggers.OuterXml;
    planned_action=$document.Task.Actions.OuterXml;implementation=$files;
    schedule='daily 07:00 Asia/Shanghai; weekly full/daily incremental chosen by Python';
    activation_gate='Explicit 07:00 window coordination, native /backups/ disk, isolated restore proof, no active backup/repair workers';
    principal_note='Original login identity preserved; InteractiveToken requires the user to stay logged on'}
  Save-Json (Join-Path $BackupDir 'plan.json') $plan
  Write-Output "Plan saved; scheduled task unchanged: $BackupDir"
  Write-Output "Old: monthly day 1 at 03:00. Target: daily 07:00, staged disabled. Login type: $($document.Task.Principals.Principal.LogonType)"
  exit 0
}
if (-not $BackupDir) { throw 'BackupDir from a reviewed Plan is required' }
$BackupDir = (Resolve-Path -LiteralPath $BackupDir).Path
$plan = Get-Content -LiteralPath (Join-Path $BackupDir 'plan.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($plan.name -ne $taskName -or $plan.task_path -ne $taskPath -or $plan.target -ne $TargetRoot) { throw 'Plan identity or target differs' }
if ($Mode -ne 'Rollback' -and $plan.version -ne $version) { throw 'Checkout changed; generate a new Plan.' }
foreach ($pair in @(@($plan.original_file,$plan.original_sha256),@($plan.planned_file,$plan.planned_sha256))) {
  if ([IO.Path]::GetFileName($pair[0]) -ne $pair[0]) { throw 'XML evidence must use a local filename' }
  if ((Hash-File (Join-Path $BackupDir $pair[0])) -ne $pair[1]) { throw 'Original/planned XML evidence changed' }
}
if ($Mode -ne 'Rollback') {
  foreach ($entry in $plan.implementation) {
    if ($entry.file -notin $implementation -or (Hash-File (Join-Path $TargetRoot $entry.file)) -ne $entry.sha256) { throw 'Backup implementation changed after planning' }
  }
  if (@($plan.implementation).Count -ne $implementation.Count) { throw 'Incomplete implementation hash evidence' }
}
$original = [IO.File]::ReadAllText((Join-Path $BackupDir $plan.original_file),[Text.Encoding]::Unicode)
$planned = [IO.File]::ReadAllText((Join-Path $BackupDir $plan.planned_file),[Text.Encoding]::Unicode)
[xml]$current = Get-Definition
[xml]$expected = $planned
function Restore-Original {
  Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Xml $original -Force | Out-Null
}
if ($Mode -eq 'Verify') {
  if (-not (Same-DefinitionSections $current $expected)) { throw 'Actions/triggers/principal differ from planned definition' }
  Write-Output "Definition verified. State: $((Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath).State)"
  exit 0
}
Assert-NoWorkers
if ($Mode -eq 'Rollback') {
  [xml]$originalDocument=$original
  if (-not (Same-DefinitionSections $current $expected) -and -not (Same-DefinitionSections $current $originalDocument)) { throw 'Task changed outside this migration; refuse to overwrite unrelated edits.' }
  Restore-Original
  Write-Output 'Original XML including monthly trigger and login identity restored.'
  exit 0
}
if ($Mode -eq 'Activate') {
  if (-not $BackupWindowCoordinated) { throw 'Coordinate the 07:00 backup resource window, then explicitly pass -BackupWindowCoordinated.' }
  if (-not (Same-DefinitionSections $current $expected)) { throw 'Task differs from staged plan' }
  Assert-BackupReady
  if ($plan.enabled) { Enable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null }
  Write-Output 'Daily backup schedule activated according to original enabled state; no immediate task execution.'
  exit 0
}
if ((Get-Definition) -ne $original) { throw 'Original task changed after planning; regenerate plan.' }
try {
  Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
  Assert-NoWorkers
  Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Xml $planned -Force | Out-Null
  [xml]$installed = Get-Definition
  if (-not (Same-DefinitionSections $installed $expected) -or (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath).Settings.Enabled) { throw 'Staged definition was not installed disabled' }
} catch { $failure=$_; Restore-Original; throw $failure }
Write-Output 'Daily native backup definition staged disabled; Activate is a separately coordinated step.'
