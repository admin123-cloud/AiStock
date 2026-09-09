param(
  [string]$PythonExe = "F:\python3.10\python.exe",
  [string]$TaskPrefix = "AiStock QMT xtquant",
  [string]$IntradayAt = "09:25",
  [int]$IntradayRepeatMinutes = 5,
  [string]$IntradayActiveStart = "09:25",
  [string]$IntradayActiveEnd = "15:10",
  [string]$IntradayPeriods = "5m,15m,30m,60m",
  [string]$IntradayUniverse = "stock,index",
  [int]$IntradayRefreshCodes = 0,
  [int]$IntradayCodeChunkSize = 1,
  [int]$IntradayBatchTimeoutSec = 180,
  [string]$AfterCloseAt = "16:00",
  [int]$AfterCloseRepeatMinutes = 30,
  [string]$AfterCloseActiveEnd = "18:30",
  [int]$AfterCloseMaxRepairCodes = 6400,
  [int]$AfterCloseMinuteBatchSize = 100,
  [string]$DailyCoverageAfterCloseAt = "16:10",
  [string]$DailyCoverageAt = "00:05",
  [int]$DailyCoverageMaxRepairCodes = 60,
  [string]$NightRepairAt = "00:40",
  [int]$NightRepairRepeatMinutes = 15,
  [string]$NightRepairActiveEnd = "06:30",
  [int]$NightRepairStartDateOffsetDays = -8,
  [int]$NightRepairEndDateOffsetDays = -2,
  [int]$NightRepairMaxRepairCodes = 6400,
  [int]$RepairCodeChunkSize = 40,
  [int]$RepairWorkers = 3,
  [int]$MinuteBatchSize = 1,
  [int]$MinuteChunkTimeoutSec = 1200,
  [int]$MinuteTimeoutSec = 10800
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunnerScript = Join-Path $RootDir "scripts\run_qmt_xtquant_collector.ps1"
if (-not (Test-Path -LiteralPath $RunnerScript)) {
  throw "QMT xtquant collector runner not found: $RunnerScript"
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function New-CollectorAction {
  param(
    [string]$Mode = "minute-gap-repair",
    [string]$Scenario,
    [string]$Periods,
    [string]$Universe,
    [int]$MaxRepairCodes = 0,
    [int]$CodeChunkSize = 0,
    [int]$RepairWorkersOverride = 0,
    [int]$MinuteBatchSizeOverride = 0,
    [int]$StartDateOffsetDays = 0,
    [int]$EndDateOffsetDays = 0,
    [int]$MinuteBatchTimeoutSec = 0,
    [string]$ActiveStart = "",
    [string]$ActiveEnd = "",
    [switch]$ContinuousUntilWindowEnd,
    [switch]$RequireAfterCloseFinalValidation,
    [switch]$ResumeAfterCloseOnValidationFailure,
    [switch]$RetryAfterCloseSourceEmpty,
    [switch]$SkipTimeWindowCheck
  )

  $Args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`" -PythonExe `"$PythonExe`" -Mode `"$Mode`" -Scenario `"$Scenario`" -Periods `"$Periods`" -Universe `"$Universe`" -StartDateOffsetDays $StartDateOffsetDays -EndDateOffsetDays $EndDateOffsetDays"
  if ($MaxRepairCodes -gt 0) {
    $EffectiveCodeChunkSize = $RepairCodeChunkSize
    if ($CodeChunkSize -gt 0) {
      $EffectiveCodeChunkSize = $CodeChunkSize
    }
    $EffectiveRepairWorkers = $RepairWorkers
    if ($RepairWorkersOverride -gt 0) {
      $EffectiveRepairWorkers = $RepairWorkersOverride
    }
    $EffectiveMinuteBatchSize = $MinuteBatchSize
    if ($MinuteBatchSizeOverride -gt 0) {
      $EffectiveMinuteBatchSize = $MinuteBatchSizeOverride
    }
    $Args = "$Args -MaxRepairCodes $MaxRepairCodes -RepairCodeChunkSize $EffectiveCodeChunkSize -RepairWorkers $EffectiveRepairWorkers -MinuteBatchSize $EffectiveMinuteBatchSize -MinuteChunkTimeoutSec $MinuteChunkTimeoutSec -MinuteTimeoutSec $MinuteTimeoutSec"
  }
  if ($MinuteBatchTimeoutSec -gt 0) {
    $Args = "$Args -MinuteBatchTimeoutSec $MinuteBatchTimeoutSec"
  }
  if ($ContinuousUntilWindowEnd) {
    $Args = "$Args -ContinuousUntilWindowEnd"
  }
  if ($RequireAfterCloseFinalValidation) {
    $Args = "$Args -RequireAfterCloseFinalValidation"
  }
  if ($ResumeAfterCloseOnValidationFailure) {
    $Args = "$Args -ResumeAfterCloseOnValidationFailure"
  }
  if ($RetryAfterCloseSourceEmpty) {
    $Args = "$Args -RetryAfterCloseSourceEmpty"
  }
  if ($SkipTimeWindowCheck) {
    $Args = "$Args -SkipTimeWindowCheck"
  } else {
    if (-not $ActiveStart) { $ActiveStart = $IntradayActiveStart }
    if (-not $ActiveEnd) { $ActiveEnd = $IntradayActiveEnd }
    $Args = "$Args -ActiveStart `"$ActiveStart`" -ActiveEnd `"$ActiveEnd`""
  }
  return New-ScheduledTaskAction -Execute $PowerShell -Argument $Args -WorkingDirectory $RootDir
}

function Register-CollectorTask {
  param(
    [string]$TaskName,
    [string]$Description,
    [object]$Action,
    [object]$Trigger
  )

  $Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
  $Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description $Description `
    -Force | Out-Null
}

function Resolve-CollectorStartAt {
  param(
    [datetime]$Now,
    [string]$At,
    [string]$ActiveStart,
    [string]$ActiveEnd,
    [bool]$AllowWeekend = $false
  )
  $StartTime = [datetime]::ParseExact($At, "HH:mm", $null)
  $StartAt = $Now.Date.AddHours($StartTime.Hour).AddMinutes($StartTime.Minute)
  $ActiveStartTime = [datetime]::ParseExact($ActiveStart, "HH:mm", $null)
  $ActiveEndTime = [datetime]::ParseExact($ActiveEnd, "HH:mm", $null)
  $WindowStart = $Now.Date.AddHours($ActiveStartTime.Hour).AddMinutes($ActiveStartTime.Minute)
  $WindowEnd = $Now.Date.AddHours($ActiveEndTime.Hour).AddMinutes($ActiveEndTime.Minute)
  if ($WindowEnd -lt $WindowStart) {
    $WindowEnd = $WindowEnd.AddDays(1)
  }
  $WeekendBlocked = (-not $AllowWeekend) -and ($Now.DayOfWeek -eq "Saturday" -or $Now.DayOfWeek -eq "Sunday")
  if ($StartAt -lt $Now) {
    if ((-not $WeekendBlocked) -and $Now -ge $WindowStart -and $Now -le $WindowEnd) {
      return $Now.AddMinutes(1)
    }
    return $StartAt.AddDays(1)
  }
  return $StartAt
}

function New-TaskWindowTriggerXml {
  param(
    [datetime]$StartAt,
    [int]$RepeatMinutes,
    [string]$ActiveStart,
    [string]$ActiveEnd,
    [string[]]$DaysOfWeek
  )

  $WindowStartTime = [datetime]::ParseExact($ActiveStart, "HH:mm", $null)
  $WindowEndTime = [datetime]::ParseExact($ActiveEnd, "HH:mm", $null)
  $WindowStart = $StartAt.Date.AddHours($WindowStartTime.Hour).AddMinutes($WindowStartTime.Minute)
  $WindowEnd = $StartAt.Date.AddHours($WindowEndTime.Hour).AddMinutes($WindowEndTime.Minute)
  if ($WindowEnd -lt $WindowStart) {
    $WindowEnd = $WindowEnd.AddDays(1)
  }
  $Duration = $WindowEnd - $WindowStart
  $DurationIso = "PT{0}H{1}M" -f [int]$Duration.TotalHours, $Duration.Minutes
  $DayXml = ($DaysOfWeek | ForEach-Object { "<$_ />" }) -join ""
  $StartBoundary = $StartAt.ToString("yyyy-MM-ddTHH:mm:ss")

  return "<Triggers xmlns=`"http://schemas.microsoft.com/windows/2004/02/mit/task`"><CalendarTrigger><StartBoundary>$StartBoundary</StartBoundary><Repetition><Interval>PT${RepeatMinutes}M</Interval><Duration>$DurationIso</Duration></Repetition><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>$DayXml</DaysOfWeek></ScheduleByWeek></CalendarTrigger></Triggers>"
}

function New-TaskWeeklyTriggerXml {
  param(
    [datetime]$StartAt,
    [string[]]$DaysOfWeek
  )

  $DayXml = ($DaysOfWeek | ForEach-Object { "<$_ />" }) -join ""
  $StartBoundary = $StartAt.ToString("yyyy-MM-ddTHH:mm:ss")
  return "<Triggers xmlns=`"http://schemas.microsoft.com/windows/2004/02/mit/task`"><CalendarTrigger><StartBoundary>$StartBoundary</StartBoundary><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>$DayXml</DaysOfWeek></ScheduleByWeek></CalendarTrigger></Triggers>"
}

function Set-RegisteredTaskWindowTrigger {
  param(
    [string]$TaskName,
    [string]$TriggerXml
  )

  $TaskXml = Export-ScheduledTask -TaskName $TaskName
  $TaskXml = [regex]::Replace($TaskXml, "<Triggers[\s\S]*?</Triggers>", $TriggerXml)
  Register-ScheduledTask -TaskName $TaskName -Xml $TaskXml -Force | Out-Null
}

$Now = Get-Date
$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$NightDays = @("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
$IntradayStartAt = Resolve-CollectorStartAt -Now $Now -At $IntradayAt -ActiveStart $IntradayActiveStart -ActiveEnd $IntradayActiveEnd
$IntradayTrigger = New-ScheduledTaskTrigger -Once -At $IntradayStartAt
Register-CollectorTask `
  -TaskName "$TaskPrefix Intraday Collector" `
  -Description "Run QMT full-push intraday aggregator during the A-share session. It writes same-day daily snapshots and aggregates 5m/15m/30m/60m minute bars to ClickHouse." `
  -Action (New-CollectorAction -Mode "intraday-fullpush-aggregate" -Scenario "intraday" -Periods $IntradayPeriods -Universe $IntradayUniverse -MaxRepairCodes $IntradayRefreshCodes -CodeChunkSize $IntradayCodeChunkSize -MinuteBatchTimeoutSec $IntradayBatchTimeoutSec) `
  -Trigger $IntradayTrigger
Set-RegisteredTaskWindowTrigger `
  -TaskName "$TaskPrefix Intraday Collector" `
  -TriggerXml (New-TaskWeeklyTriggerXml -StartAt $IntradayStartAt -DaysOfWeek $Weekdays)

$AfterStartAt = Resolve-CollectorStartAt -Now $Now -At $AfterCloseAt -ActiveStart $AfterCloseAt -ActiveEnd $AfterCloseActiveEnd
$AfterCloseTrigger = New-ScheduledTaskTrigger -Once -At $AfterStartAt
# Keep the after-close QMT fallback single-process and in small chunks. The
# primary 5m/full-push + derived-frame path handles the market in bulk; this
# fallback only fetches residual incomplete codes and must not overload QMT.
Register-CollectorTask `
  -TaskName "$TaskPrefix After Close Repair" `
  -Description "Run QMT after-close finalization from 16:00; retry every 30 minutes within the active window until minute validation closes." `
  -Action (New-CollectorAction -Mode "after-close-finalize" -Scenario "after-close" -Periods "5m,15m,30m,60m" -Universe "stock,index" -MaxRepairCodes $AfterCloseMaxRepairCodes -CodeChunkSize 10 -RepairWorkersOverride 1 -MinuteBatchSizeOverride $AfterCloseMinuteBatchSize -ActiveStart $AfterCloseAt -ActiveEnd $AfterCloseActiveEnd -ContinuousUntilWindowEnd) `
  -Trigger $AfterCloseTrigger
Set-RegisteredTaskWindowTrigger `
  -TaskName "$TaskPrefix After Close Repair" `
  -TriggerXml (New-TaskWindowTriggerXml -StartAt $AfterStartAt -RepeatMinutes $AfterCloseRepeatMinutes -ActiveStart $AfterCloseAt -ActiveEnd $AfterCloseActiveEnd -DaysOfWeek $Weekdays)

$DailyCoverageStartAt = Resolve-CollectorStartAt -Now $Now -At $DailyCoverageAt -ActiveStart $DailyCoverageAt -ActiveEnd $DailyCoverageAt -AllowWeekend $true
$DailyCoverageTrigger = New-ScheduledTaskTrigger -Once -At $DailyCoverageStartAt
Register-CollectorTask `
  -TaskName "$TaskPrefix Daily Coverage Repair" `
  -Description "Run bounded QMT daily-bar coverage repair on the Windows host before the backend publishes the overnight audit result." `
  -Action (New-CollectorAction -Mode "daily-coverage-repair" -Scenario "daily-coverage" -Periods "1d" -Universe "stock" -MaxRepairCodes $DailyCoverageMaxRepairCodes -CodeChunkSize 30 -ActiveStart $DailyCoverageAt -ActiveEnd $DailyCoverageAt -SkipTimeWindowCheck) `
  -Trigger $DailyCoverageTrigger
$DailyCoverageAfterCloseStartAt = Resolve-CollectorStartAt -Now $Now -At $DailyCoverageAfterCloseAt -ActiveStart $DailyCoverageAfterCloseAt -ActiveEnd $DailyCoverageAfterCloseAt
$DailyCoverageTriggerXml = (New-TaskWeeklyTriggerXml -StartAt $DailyCoverageStartAt -DaysOfWeek $NightDays) + (New-TaskWeeklyTriggerXml -StartAt $DailyCoverageAfterCloseStartAt -DaysOfWeek $Weekdays)
Set-RegisteredTaskWindowTrigger -TaskName "$TaskPrefix Daily Coverage Repair" -TriggerXml $DailyCoverageTriggerXml

$NightStartAt = Resolve-CollectorStartAt -Now $Now -At $NightRepairAt -ActiveStart $NightRepairAt -ActiveEnd $NightRepairActiveEnd -AllowWeekend $true
$NightTrigger = New-ScheduledTaskTrigger -Once -At $NightStartAt
# Historical audit is cache-first. If the after-close run did not close, first
# resume that date in the same night window, revalidate it, then continue with
# the preceding-week audit. This preserves the weekly scope without deadlocking
# the automatic recovery path on a missing final-validation artifact.
Register-CollectorTask `
  -TaskName "$TaskPrefix Night Rolling Repair" `
  -Description "Resume and revalidate an unclosed previous after-close run, then audit and repair the preceding week of QMT minute data, including one retry of persisted QMT-empty 5m sources." `
  -Action (New-CollectorAction -Scenario "history" -Periods "5m,15m,30m,60m" -Universe "stock,index" -MaxRepairCodes $NightRepairMaxRepairCodes -CodeChunkSize 10 -RepairWorkersOverride 1 -MinuteBatchSizeOverride 1 -StartDateOffsetDays $NightRepairStartDateOffsetDays -EndDateOffsetDays $NightRepairEndDateOffsetDays -ActiveStart $NightRepairAt -ActiveEnd $NightRepairActiveEnd -ContinuousUntilWindowEnd -RequireAfterCloseFinalValidation -ResumeAfterCloseOnValidationFailure -RetryAfterCloseSourceEmpty) `
  -Trigger $NightTrigger
Set-RegisteredTaskWindowTrigger `
  -TaskName "$TaskPrefix Night Rolling Repair" `
  -TriggerXml (New-TaskWeeklyTriggerXml -StartAt $NightStartAt -DaysOfWeek $NightDays)

Write-Output "Installed QMT xtquant collection schedule:"
Write-Output "  - $TaskPrefix Intraday Collector: one long-lived run starts $IntradayAt on weekdays, active window $IntradayActiveStart~$IntradayActiveEnd, mode=intraday-fullpush-aggregate, periods=$IntradayPeriods"
Write-Output "  - $TaskPrefix After Close Repair: one long-lived run starts $AfterCloseAt on weekdays, active window $AfterCloseAt~$AfterCloseActiveEnd, mode=after-close-finalize"
Write-Output "  - $TaskPrefix Daily Coverage Repair: host-side 1d bounded repair starts $DailyCoverageAt before the backend audit"
Write-Output "  - $TaskPrefix Daily Coverage Repair: second trigger for closed-day repair starts $DailyCoverageAfterCloseAt on weekdays using the same abnormal-data policy"
Write-Output "  - $TaskPrefix Night Rolling Repair: one long-lived run starts $NightRepairAt, active window $NightRepairAt~$NightRepairActiveEnd, date_offset=$NightRepairStartDateOffsetDays~$NightRepairEndDateOffsetDays"
Write-Output "Runner script: $RunnerScript"
Write-Output "PythonExe: $PythonExe"
