param(
  [ValidateSet('Plan','Apply','Activate','Verify','Rollback')][string]$Mode='Plan',
  [string]$BackupDir,
  [string]$PythonExe='F:\python3.10\python.exe'
)
$ErrorActionPreference='Stop'
$taskName='AiStockMonitor-Preopen'
$sourceScript='F:\AiDevelop\dsh-wechat-workspace\dify-monitor\triggers\Invoke-AistockMonitor.ps1'
$stablePowerShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $stablePowerShell)) { throw 'Stable Windows PowerShell executable unavailable.' }
function Assert-Idle {
  if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') { throw 'Dify task is running; no interruption is allowed.' }
}
if ($Mode -eq 'Plan') {
  if (-not $BackupDir) { $BackupDir=Join-Path 'F:\Stock\AiStockData\artifacts\dify-launcher-migration' (Get-Date -Format 'yyyyMMdd-HHmmss') }
  if (Test-Path -LiteralPath $BackupDir) { throw 'Use a fresh immutable backup directory.' }
  New-Item -ItemType Directory -Path $BackupDir | Out-Null
  [IO.File]::WriteAllText((Join-Path $BackupDir 'task.original.xml'),(Export-ScheduledTask -TaskName $taskName),[Text.Encoding]::Unicode)
  & $PythonExe (Join-Path $PSScriptRoot 'plan_dify_launcher.py') $BackupDir --script $sourceScript --powershell $stablePowerShell
  if ($LASTEXITCODE -ne 0) { throw 'Dify launcher plan failed; production unchanged.' }
  $plan=Get-Content -LiteralPath (Join-Path $BackupDir 'plan.json') -Raw -Encoding utf8 | ConvertFrom-Json
  $compatible=Join-Path $BackupDir $plan.compatible_file
  $escaped=$compatible.Replace("'","''")
  $parse='$tokens=$null;$errors=$null;$null=[System.Management.Automation.Language.Parser]::ParseFile('''+$escaped+''',[ref]$tokens,[ref]$errors);if($errors.Count){exit 2};exit 0'
  & $stablePowerShell -NoProfile -NonInteractive -Command $parse
  if ($LASTEXITCODE -ne 0) { throw 'Compatible source does not parse under Windows PowerShell 5; do not apply.' }
  [IO.File]::WriteAllText((Join-Path $BackupDir 'ps5-parse-passed.txt'),$plan.compatible_sha256,[Text.UTF8Encoding]::new($false))
  Write-Output "Plan/PS5 parse only; dispatcher and notifications NOT executed: $BackupDir"
  exit 0
}
if (-not $BackupDir) { throw 'BackupDir required.' }
$plan=Get-Content -LiteralPath (Join-Path $BackupDir 'plan.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($plan.task -ne $taskName -or $plan.script -ne $sourceScript) { throw 'Unexpected migration target.' }
foreach ($item in @(@('task.original.xml',$plan.original_sha256),@('task.planned.xml',$plan.planned_sha256),@($plan.compatible_file,$plan.compatible_sha256))) {
  if ([IO.Path]::GetFileName($item[0]) -ne $item[0] -or (Get-FileHash -LiteralPath (Join-Path $BackupDir $item[0])).Hash.ToLowerInvariant() -ne $item[1]) { throw 'Migration evidence hash mismatch.' }
}
if ((Get-Content -LiteralPath (Join-Path $BackupDir 'ps5-parse-passed.txt') -Raw) -ne $plan.compatible_sha256) { throw 'PS5 parser validation missing.' }
if ([IO.Path]::GetDirectoryName($plan.compatible_path) -ne [IO.Path]::GetDirectoryName($sourceScript)) { throw 'Compatible launcher must stay beside the original script.' }
function Assert-Planned {
  [xml]$current=Export-ScheduledTask -TaskName $taskName
  [xml]$desired=[IO.File]::ReadAllText((Join-Path $BackupDir 'task.planned.xml'),[Text.Encoding]::Unicode)
  foreach($section in @('Actions','Triggers','Principals')) {
    if($current.Task.$section.InnerXml -ne $desired.Task.$section.InnerXml) { throw "Dify $section not migrated." }
  }
  if ((Get-FileHash -LiteralPath $plan.compatible_path).Hash.ToLowerInvariant() -ne $plan.compatible_sha256) { throw 'Installed compatible source changed.' }
}
if ($Mode -eq 'Verify') { Assert-Planned; Write-Output 'Dify launcher and daily trigger definitions verified; no flow executed.'; exit 0 }
Assert-Idle
if ($Mode -eq 'Rollback') {
  Register-ScheduledTask -TaskName $taskName -Xml ([IO.File]::ReadAllText((Join-Path $BackupDir 'task.original.xml'),[Text.Encoding]::Unicode)) -Force | Out-Null
  Write-Output 'Original task XML restored. Compatible generated copy retained for audit.'
  exit 0
}
if ((Get-FileHash -LiteralPath $sourceScript).Hash.ToLowerInvariant() -ne $plan.source_sha256) { throw 'Original dispatcher changed after planning.' }
if ($Mode -eq 'Activate') {
  Assert-Planned
  Enable-ScheduledTask -TaskName $taskName | Out-Null
  Write-Output 'Dify schedule activated for future triggers; its existing notifications can run. No immediate flow started.'
  exit 0
}
$original=[IO.File]::ReadAllText((Join-Path $BackupDir 'task.original.xml'),[Text.Encoding]::Unicode)
if ((Export-ScheduledTask -TaskName $taskName) -ne $original) { throw 'Task XML changed after planning.' }
if (Test-Path -LiteralPath $plan.compatible_path) {
  if ((Get-FileHash -LiteralPath $plan.compatible_path).Hash.ToLowerInvariant() -ne $plan.compatible_sha256) { throw 'Existing compatible path has different contents.' }
} else { Copy-Item -LiteralPath (Join-Path $BackupDir $plan.compatible_file) -Destination $plan.compatible_path }
try {
  Disable-ScheduledTask -TaskName $taskName | Out-Null
  Assert-Idle
  Register-ScheduledTask -TaskName $taskName -Xml ([IO.File]::ReadAllText((Join-Path $BackupDir 'task.planned.xml'),[Text.Encoding]::Unicode)) -Force | Out-Null
  Assert-Planned
} catch { $failure=$_;Register-ScheduledTask -TaskName $taskName -Xml $original -Force | Out-Null;throw $failure }
Write-Output 'Stable PS5 launcher and daily triggers staged Disabled. Activate is a separate notification-owner decision.'
