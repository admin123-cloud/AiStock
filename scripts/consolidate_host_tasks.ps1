param(
    [ValidateSet('Plan','Apply','Rollback')][string]$Mode='Plan',
    [string]$BackupDir,
    [string]$SourceRoot='F:\Stock\AiStock-core',
    [string]$PythonExe='F:\python3.10\python.exe',
    [string]$PythonwExe='F:\python3.10\pythonw.exe',
    [string]$RuntimeRoot='F:\Stock\AiStockData\data\runtime'
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
$taskEncoding=[System.Text.Encoding]::Unicode
if ($Mode -eq 'Plan') {
    if (-not $BackupDir) { $BackupDir=Join-Path 'F:\Stock\AiStockData\artifacts\task-consolidation' (Get-Date -Format 'yyyyMMdd-HHmmss') }
    if (Test-Path -LiteralPath $BackupDir) { throw 'Use a new backup directory; never overwrite rollback XML' }
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $taskInventory=@(Get-ScheduledTask | Where-Object {$_.TaskName -like '*AiStock*'} | ForEach-Object {
        $taskName=$_.TaskName
        $taskFile=($taskName -replace '[^a-zA-Z0-9_-]','_')+'.xml'
        $taskXml=Export-ScheduledTask -TaskName $taskName
        [IO.File]::WriteAllText((Join-Path $BackupDir $taskFile),$taskXml,$taskEncoding)
        @{name=$taskName; file=$taskFile; state=[string]$_.State; sha256=(Get-FileHash -LiteralPath (Join-Path $BackupDir $taskFile)).Hash}
    })
    $taskInventory | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BackupDir 'inventory.json') -Encoding UTF8
    & $PythonExe (Join-Path $PSScriptRoot 'plan_task_consolidation.py') $BackupDir --source-root $SourceRoot --pythonw $PythonwExe
    if ($LASTEXITCODE -ne 0) { throw 'XML plan validation failed; production tasks unchanged' }
    Write-Output "Plan and recovery XML: $BackupDir"
    exit 0
}
if (-not $BackupDir) { throw 'BackupDir is required' }
$taskPlan=Get-Content -LiteralPath (Join-Path $BackupDir 'plan.json') -Raw | ConvertFrom-Json
$taskInventory=Get-Content -LiteralPath (Join-Path $BackupDir 'inventory.json') -Raw | ConvertFrom-Json
$taskAffected=@($taskPlan.groups | ForEach-Object {$_.sources})+@($taskPlan.retired)
foreach($taskOriginal in $taskInventory | Where-Object {$_.name -in $taskAffected}) {
    if ((Get-FileHash -LiteralPath (Join-Path $BackupDir $taskOriginal.file)).Hash -ne $taskOriginal.sha256) { throw 'Rollback XML hash mismatch' }
}
function Restore-TaskDefinitions {
    foreach($taskName in $taskAffected) {
        $taskCurrent=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if($taskCurrent -and $taskCurrent.State -eq 'Running') { throw "Wait for running task before rollback: $taskName" }
    }
    foreach($taskOriginal in $taskInventory | Where-Object {$_.name -in $taskAffected}) {
        Register-ScheduledTask -TaskName $taskOriginal.name -Xml ([IO.File]::ReadAllText((Join-Path $BackupDir $taskOriginal.file),$taskEncoding)) -Force | Out-Null
    }
}
if($Mode -eq 'Rollback') { Restore-TaskDefinitions; $taskTransitionFile=Join-Path $RuntimeRoot 'operations/task_transitions.json'; if(Test-Path -LiteralPath $taskTransitionFile) { @{generated_at=(Get-Date).ToString('o');retired=@();rolled_back_from=$BackupDir} | ConvertTo-Json | Set-Content -LiteralPath $taskTransitionFile -Encoding UTF8 }; Write-Output 'Original definitions restored; verify runtime ownership before resuming.'; exit 0 }
foreach($taskName in $taskAffected) {
    $taskCurrent=Get-ScheduledTask -TaskName $taskName
    if($taskCurrent.State -eq 'Running') { throw "Task currently running; do not interrupt: $taskName" }
    if($taskName -in $taskPlan.retired -and $taskCurrent.State -ne 'Disabled') { throw "Retirement candidate is no longer disabled: $taskName" }
    $taskOriginal=$taskInventory | Where-Object {$_.name -eq $taskName}
    if((Export-ScheduledTask -TaskName $taskName) -ne [IO.File]::ReadAllText((Join-Path $BackupDir $taskOriginal.file),$taskEncoding)) { throw "Definition changed since planning: $taskName" }
}
$taskRetired=@()
try {
    foreach($taskGroup in $taskPlan.groups) {
        foreach($taskName in $taskGroup.sources) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
        [xml]$taskMerged=[IO.File]::ReadAllText((Join-Path $BackupDir $taskGroup.file),$taskEncoding)
        Register-ScheduledTask -TaskName $taskGroup.primary -Xml $taskMerged.OuterXml -Force | Out-Null
        [xml]$taskVerify=Export-ScheduledTask -TaskName $taskGroup.primary
        foreach($taskSection in @('Actions','Principals')) { if($taskVerify.Task.$taskSection.InnerXml -ne $taskMerged.Task.$taskSection.InnerXml) { throw "Merged $taskSection mismatch" } }
        if($taskVerify.Task.Triggers.ChildNodes.Count -ne $taskMerged.Task.Triggers.ChildNodes.Count) { throw 'Merged trigger count mismatch' }
        foreach($taskName in $taskGroup.sources | Where-Object {$_ -ne $taskGroup.primary}) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            $taskRetired+=@{name=$taskName;replacement=$taskGroup.primary;retired_at=(Get-Date).ToString('o')}
        }
    }
    foreach($taskName in $taskPlan.retired) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        $taskRetired+=@{name=$taskName;replacement='现有生产能力 / 受控手动兜底';retired_at=(Get-Date).ToString('o')}
    }
} catch {
    $taskFailure=$_
    Restore-TaskDefinitions
    throw $taskFailure
}
$taskTransitionDir=Join-Path $RuntimeRoot 'operations'
New-Item -ItemType Directory -Force -Path $taskTransitionDir | Out-Null
@{generated_at=(Get-Date).ToString('o');retired=$taskRetired;backup_dir=$BackupDir;groups=$taskPlan.groups;notifications_enabled_by_migration=$false} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $taskTransitionDir 'task_transitions.json') -Encoding UTF8
Write-Output "Merged definitions verified. Retired registrations: $($taskRetired.Count). Backup: $BackupDir"
