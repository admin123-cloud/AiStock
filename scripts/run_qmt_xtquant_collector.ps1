param(
  [string]$PythonExe = "F:\python3.10\python.exe",
  [string]$Mode = "minute-gap-repair",
  [string]$Scenario = "intraday",
  [string]$Periods = "5m,15m,30m,60m",
  [string]$Codes = "",
  [string]$Universe = "index",
  [string]$StartDate = "",
  [string]$EndDate = "",
  [int]$StartDateOffsetDays = 0,
  [int]$EndDateOffsetDays = 0,
  [int]$MaxRepairCodes = 0,
  [int]$RepairCodeChunkSize = 1,
  [int]$RepairWorkers = 1,
  [int]$MinuteBatchSize = 1,
  [int]$MinuteBatchTimeoutSec = 120,
  [int]$MinuteChunkTimeoutSec = 180,
  [int]$MinuteTimeoutSec = 7200,
  [int]$FullPushDurationSec = 0,
  [switch]$FullPushDryRun,
  [int]$RepairCodeOffset = -1,
  [string]$RollingStateFile = "",
  [string]$ActiveStart = "09:30",
  [string]$ActiveEnd = "15:10",
  [switch]$ContinuousUntilWindowEnd,
  [switch]$RequireAfterCloseFinalValidation,
  [switch]$ResumeAfterCloseOnValidationFailure,
  [switch]$RetryAfterCloseSourceEmpty,
  [switch]$AllowWeekend,
  [int]$ContinuousSleepSeconds = 5,
  [string]$DisableTaskOnNoProgress = "",
  [switch]$SkipTimeWindowCheck,
  [string]$LogFile = "F:\Stock\AiStockData\data\runtime\qmt_xtquant_collector.log"
)

$ErrorActionPreference = "Stop"
if ($Mode -eq 'daily-maintenance') {
  $Mode = if ((Get-Date).Hour -eq 18) { 'reference-maintenance' } else { 'daily-coverage-repair' }
}
$DailyStartExplicit = $PSBoundParameters.ContainsKey('StartDate')
$DailyEndExplicit = $PSBoundParameters.ContainsKey('EndDate')

if (-not $env:AISTOCK_QMT_ROOT) {
  $env:AISTOCK_QMT_ROOT = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("RDpc5Zu96YeRUU1UXOWbvemHkeivgeWIuFFNVOS6pOaYk+errw=="))
}
if (-not $env:AISTOCK_QMT_USERDATA) {
  $env:AISTOCK_QMT_USERDATA = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("RDpc5Zu96YeRUU1UXOWbvemHkeivgeWIuFFNVOS6pOaYk+err1x1c2VyZGF0YV9taW5p"))
}
if (-not $env:AISTOCK_QMT_QUOTE_HOST) {
  $env:AISTOCK_QMT_QUOTE_HOST = "127.0.0.1"
}
if (-not $env:AISTOCK_QMT_QUOTE_PORT) {
  $env:AISTOCK_QMT_QUOTE_PORT = "58610"
}

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$CollectorScript = Join-Path $RootDir "scripts\qmt_xtquant_data_source_task.py"
if (-not (Test-Path -LiteralPath $CollectorScript)) {
  throw "QMT xtquant unified collector not found: $CollectorScript"
}
$DailyBackfillScript = Join-Path $RootDir "scripts\qmtmini_daily_backfill_validate.py"
if (-not (Test-Path -LiteralPath $DailyBackfillScript)) {
  throw "QMT daily worker not found: $DailyBackfillScript"
}
$MinuteBackfillScript = Join-Path $RootDir "scripts\qmt_xtquant_minute_backfill_validate.py"
if (-not (Test-Path -LiteralPath $MinuteBackfillScript)) {
  throw "QMT xtquant minute worker not found: $MinuteBackfillScript"
}
$DatadirIntradayScript = Join-Path $RootDir "scripts\import_qmt_datadir_intraday_to_clickhouse.py"
if (-not (Test-Path -LiteralPath $DatadirIntradayScript)) {
  throw "QMT datadir intraday importer not found: $DatadirIntradayScript"
}
$FullPushAggregatorScript = Join-Path $RootDir "scripts\qmt_fullpush_intraday_aggregator.py"
if (-not (Test-Path -LiteralPath $FullPushAggregatorScript)) {
  throw "QMT full-push intraday aggregator not found: $FullPushAggregatorScript"
}
$DailyCoverageScript = Join-Path $RootDir "scripts\daily_kline_coverage_maintenance.py"
if (-not (Test-Path -LiteralPath $DailyCoverageScript)) {
  throw "QMT daily coverage worker not found: $DailyCoverageScript"
}
$GovernanceScript = Join-Path $RootDir "scripts\govern_kline_history.py"
if (-not (Test-Path -LiteralPath $GovernanceScript)) {
  throw "K-line governance script not found: $GovernanceScript"
}

$LogDir = Split-Path -Parent $LogFile
if ($LogDir -and -not (Test-Path -LiteralPath $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-CollectorLog {
  param([string]$Message)
  $Ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  "[$Ts] $Message" | Out-File -LiteralPath $LogFile -Append -Encoding utf8
}

function Invoke-LoggedNative {
  param(
    [string]$Exe,
    [string[]]$Arguments
  )
  & $Exe @Arguments 2>&1 | ForEach-Object {
    $Line = [string]$_
    Write-CollectorLog $Line
  }
  return $LASTEXITCODE
}

function Get-WindowBounds {
  param(
    [datetime]$Now,
    [string]$StartText,
    [string]$EndText
  )
  $StartTime = [datetime]::ParseExact($StartText, "HH:mm", $null)
  $EndTime = [datetime]::ParseExact($EndText, "HH:mm", $null)
  $WindowStart = $Now.Date.AddHours($StartTime.Hour).AddMinutes($StartTime.Minute)
  $WindowEnd = $Now.Date.AddHours($EndTime.Hour).AddMinutes($EndTime.Minute)
  if ($WindowEnd -lt $WindowStart) {
    $WindowEnd = $WindowEnd.AddDays(1)
  }
  return [PSCustomObject]@{
    Start = $WindowStart
    End = $WindowEnd
  }
}

function Test-ActiveWindow {
  param([datetime]$Now)
  if (($Scenario -ne "history") -and -not $AllowWeekend -and ($Now.DayOfWeek -eq "Saturday" -or $Now.DayOfWeek -eq "Sunday")) {
    return [PSCustomObject]@{ Ok = $false; Reason = "outside weekday window"; End = $Now }
  }
  $Bounds = Get-WindowBounds -Now $Now -StartText $ActiveStart -EndText $ActiveEnd
  if ($Now -lt $Bounds.Start -or $Now -gt $Bounds.End) {
    return [PSCustomObject]@{ Ok = $false; Reason = "outside active window $ActiveStart~$ActiveEnd"; End = $Bounds.End }
  }
  return [PSCustomObject]@{ Ok = $true; Reason = ""; End = $Bounds.End }
}

function Get-IssueCodeCount {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return 0
  }
  try {
    $Rows = Import-Csv -LiteralPath $Path
    $Codes = @($Rows | Where-Object { $_.code } | ForEach-Object { [string]$_.code } | Sort-Object -Unique)
    return $Codes.Count
  } catch {
    return 0
  }
}

function Get-WorkerIssueCount {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return $null
  }
  try {
    $Summary = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $Worker = $Summary.minute.worker_summary
    if ($null -ne $Worker -and $null -ne $Worker.issue_count) {
      return [int]$Worker.issue_count
    }
  } catch {
    return $null
  }
  return $null
}

function Get-IntradayRefreshResult {
  param([string]$Path)
  $Result = [PSCustomObject]@{
    SelectedCodes = $null
    FailedBatches = $null
  }
  if (-not (Test-Path -LiteralPath $Path)) {
    return $Result
  }
  try {
    $Report = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $Fetch = @($Report.summaries | Where-Object { $_.phase -eq "fetch" } | Select-Object -First 1)
    if ($Fetch.Count -gt 0) {
      if ($null -ne $Fetch[0].codes) {
        $Result.SelectedCodes = [int]$Fetch[0].codes
      }
      if ($null -ne $Fetch[0].failed_batches) {
        $Result.FailedBatches = [int]$Fetch[0].failed_batches
      }
    }
  } catch {
    return $Result
  }
  return $Result
}

if (-not $StartDate) {
  $StartDate = (Get-Date).AddDays($StartDateOffsetDays).ToString("yyyy-MM-dd")
}
if (-not $EndDate) {
  $EndDate = (Get-Date).AddDays($EndDateOffsetDays).ToString("yyyy-MM-dd")
}

if (-not $SkipTimeWindowCheck) {
  $Window = Test-ActiveWindow -Now (Get-Date)
  if (-not $Window.Ok) {
    Write-CollectorLog "SKIP qmt_xtquant_collector $($Window.Reason)"
    exit 0
  }
}

function Resolve-PreviousTradingDate {
  param([datetime]$Now)

  # Midnight may follow a public holiday. Ask the same persisted trading
  # calendar used by the application instead of treating the prior calendar
  # date as a completed market session. Weekday fallback is only for a
  # temporary calendar-query outage.
  $Fallback = $Now.AddDays(-1)
  while ($Fallback.DayOfWeek -eq "Saturday" -or $Fallback.DayOfWeek -eq "Sunday") {
    $Fallback = $Fallback.AddDays(-1)
  }
  $Probe = "from datetime import datetime; from scheduler.trading_calendar import TradingCalendar; print(TradingCalendar.get_previous_trading_day(datetime.now()).strftime('%Y-%m-%d'))"
  try {
    $Lines = @(& $PythonExe -c $Probe 2>$null)
    if ($LASTEXITCODE -eq 0) {
      foreach ($Line in ($Lines | Select-Object -Reverse)) {
        $Text = ([string]$Line).Trim()
        if ($Text -match '^\d{4}-\d{2}-\d{2}$') {
          return $Text
        }
      }
    }
  } catch {
    # The fallback below is intentionally logged for auditability.
  }
  $FallbackText = $Fallback.ToString("yyyy-MM-dd")
  Write-CollectorLog "WARN qmt_xtquant_collector trade-calendar lookup failed; fallback_previous_weekday=$FallbackText"
  return $FallbackText
}

function Get-AfterCloseValidationState {
  param([string]$TradeDate)

  $ValidationPath = "F:\Stock\AiStockData\data\runtime\qmt_xtquant_collector_after-close_${TradeDate}_${TradeDate}\final_validation.json"
  if (-not (Test-Path -LiteralPath $ValidationPath)) {
    return [PSCustomObject]@{ Closed = $false; Reason = "missing"; Path = $ValidationPath }
  }
  try {
    $Validation = Get-Content -LiteralPath $ValidationPath -Raw | ConvertFrom-Json
    if ([bool]$Validation.closed) {
      return [PSCustomObject]@{ Closed = $true; Reason = "closed"; Path = $ValidationPath }
    }
    return [PSCustomObject]@{ Closed = $false; Reason = "not_closed"; Path = $ValidationPath }
  } catch {
    return [PSCustomObject]@{ Closed = $false; Reason = "unreadable"; Path = $ValidationPath; Error = $_.Exception.Message }
  }
}

function Invoke-AfterCloseRecovery {
  param([string]$TradeDate)

  # Run in a child PowerShell process: the runner uses `exit`, so invoking this
  # file in-process would terminate the night-history task before it can recheck
  # the final validation result.
  $PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  $RecoveryArgs = @(
    "-NoProfile",
    "-WindowStyle", "Hidden",
    "-ExecutionPolicy", "Bypass",
    "-File", $PSCommandPath,
    "-PythonExe", $PythonExe,
    "-Mode", "after-close-finalize",
    "-Scenario", "after-close",
    "-Periods", $Periods,
    "-Universe", $Universe,
    "-StartDate", $TradeDate,
    "-EndDate", $TradeDate,
    "-MaxRepairCodes", "$MaxRepairCodes",
    "-RepairCodeChunkSize", "$RepairCodeChunkSize",
    "-RepairWorkers", "$RepairWorkers",
    "-MinuteBatchSize", "$MinuteBatchSize",
    "-MinuteBatchTimeoutSec", "$MinuteBatchTimeoutSec",
    "-MinuteChunkTimeoutSec", "$MinuteChunkTimeoutSec",
    "-MinuteTimeoutSec", "$MinuteTimeoutSec",
    "-ActiveStart", $ActiveStart,
    "-ActiveEnd", $ActiveEnd,
    "-ContinuousUntilWindowEnd",
    "-AllowWeekend",
    "-LogFile", $LogFile
  )
  Write-CollectorLog "RECOVER qmt_xtquant_collector after-close validation reason=missing_or_open date=$TradeDate"
  $ExitCode = Invoke-LoggedNative -Exe $PowerShellExe -Arguments $RecoveryArgs
  Write-CollectorLog "END after_close_recovery exit_code=$ExitCode date=$TradeDate"
  return $ExitCode
}

if ($Scenario -eq "history") {
  $BacklogArgs = @((Join-Path $RootDir 'scripts/run_ingestion_backlog.py'))
  $BacklogExit = Invoke-LoggedNative -Exe $PythonExe -Arguments $BacklogArgs
  Write-CollectorLog "Deferred ingestion replay finished exit_code=$BacklogExit; inspect operations/ingestion_backlog.json"
}

if ($RequireAfterCloseFinalValidation -and $Scenario -ne "history") {
  $PreviousCloseDate = Resolve-PreviousTradingDate -Now (Get-Date)
  $AfterCloseState = Get-AfterCloseValidationState -TradeDate $PreviousCloseDate
  if (-not $AfterCloseState.Closed -and $ResumeAfterCloseOnValidationFailure) {
    $RecoveryExitCode = Invoke-AfterCloseRecovery -TradeDate $PreviousCloseDate
    $AfterCloseState = Get-AfterCloseValidationState -TradeDate $PreviousCloseDate
    if ($RecoveryExitCode -ne 0) {
      Write-CollectorLog "WARN qmt_xtquant_collector after-close recovery exited_nonzero=$RecoveryExitCode date=$PreviousCloseDate"
    }
  }
  if (-not $AfterCloseState.Closed) {
    $ErrorText = if ($AfterCloseState.Error) { " error=$($AfterCloseState.Error)" } else { "" }
    Write-CollectorLog "BLOCK qmt_xtquant_collector after-close validation reason=$($AfterCloseState.Reason) date=$PreviousCloseDate path=$($AfterCloseState.Path)$ErrorText"
    exit 2
  }
  Write-CollectorLog "PASS qmt_xtquant_collector after-close validation closed date=$PreviousCloseDate path=$($AfterCloseState.Path)"
}

if ($Scenario -eq "history" -and $RequireAfterCloseFinalValidation) {
  $PreviousCloseDate = Resolve-PreviousTradingDate -Now (Get-Date)
  if ($EndDate -lt $PreviousCloseDate) { $EndDate = $PreviousCloseDate }
  Write-CollectorLog "HISTORY includes previous trading day $PreviousCloseDate without global period gate"
}

$SafeScenario = $Scenario -replace '[^A-Za-z0-9_-]', '_'
$SafeDateRange = "$StartDate`_$EndDate" -replace '[^0-9A-Za-z_-]', '_'
$StableReportDir = "F:\Stock\AiStockData\data\runtime\qmt_xtquant_collector_${SafeScenario}_${SafeDateRange}"
$StableIssueFile = Join-Path $StableReportDir "issues.csv"
if (-not (Test-Path -LiteralPath $StableReportDir)) {
  New-Item -ItemType Directory -Path $StableReportDir -Force | Out-Null
}

if ($MaxRepairCodes -gt 0 -and -not $RollingStateFile) {
  $RollingStateFile = "F:\Stock\AiStockData\data\runtime\qmt_xtquant_collector_${SafeScenario}_offset.json"
}
if ($RollingStateFile) {
  $StateDir = Split-Path -Parent $RollingStateFile
  if ($StateDir -and -not (Test-Path -LiteralPath $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
  }
}
$DateKey = "$Scenario|$StartDate|$EndDate|$Periods|$Universe"

function Read-AppliedOffset {
  if ($RepairCodeOffset -ge 0) {
    return $RepairCodeOffset
  }
  if ($MaxRepairCodes -le 0 -or -not $RollingStateFile) {
    return 0
  }
  if (Test-Path -LiteralPath $RollingStateFile) {
    try {
      $State = Get-Content -LiteralPath $RollingStateFile -Raw | ConvertFrom-Json
      if ($State.date_key -eq $DateKey) {
        return [int]$State.next_offset
      }
    } catch {
      return 0
    }
  }
  return 0
}

function Write-RollingState {
  param([int]$NextOffset)
  if (-not $RollingStateFile) {
    return
  }
  @{
    date_key = $DateKey
    next_offset = $NextOffset
    last_success_at = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  } | ConvertTo-Json | Out-File -LiteralPath $RollingStateFile -Encoding utf8
}

function Invoke-CollectorOnce {
  $AppliedRepairCodeOffset = Read-AppliedOffset
  if ($Mode -eq "reference-maintenance") {
    $ReferenceArgs = @((Join-Path $RootDir 'scripts/run_reference_maintenance.py'))
    $ReferenceExit = Invoke-LoggedNative -Exe $PythonExe -Arguments $ReferenceArgs
    return [PSCustomObject]@{ ExitCode = $ReferenceExit; WorkerIssueCount = $null }
  }
  if ($Mode -eq "daily-coverage-repair") {
    $DailyScope = if ((Get-Date).Hour -ge 15) { 'latest' } else { 'year' }
    $CoverageArgs = @(
      $DailyCoverageScript,
      "--mode", "repair",
      "--scope", $DailyScope,
      "--max-repair-codes", "$MaxRepairCodes",
      "--batch-size", "$([Math]::Max(1, $RepairCodeChunkSize))"
    )
    if ($DailyStartExplicit) { $CoverageArgs += @('--start-date', $StartDate) }
    if ($DailyEndExplicit) { $CoverageArgs += @('--end-date', $EndDate) }
    Write-CollectorLog "START qmt_daily_coverage_repair max_repair_codes=$MaxRepairCodes batch_size=$([Math]::Max(1, $RepairCodeChunkSize))"
    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $CoverageArgs
    } finally {
      Pop-Location
    }
    Write-CollectorLog "END qmt_daily_coverage_repair exit_code=$ExitCode report=F:\Stock\AiStockData\data\runtime\daily_kline_coverage\latest.json"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }
  if ($Mode -eq "intraday-fullpush-aggregate") {
    $FullPushReport = Join-Path $StableReportDir "intraday_fullpush_aggregate.json"
    $NowForDuration = Get-Date
    $DurationSec = 300
    if (-not $SkipTimeWindowCheck) {
      $Bounds = Get-WindowBounds -Now $NowForDuration -StartText $ActiveStart -EndText $ActiveEnd
      $DurationSec = [Math]::Max(60, [int]($Bounds.End - $NowForDuration).TotalSeconds)
    }
    if ($FullPushDurationSec -gt 0) {
      $DurationSec = $FullPushDurationSec
    }
    Write-CollectorLog "START qmt_fullpush_intraday_aggregate duration_sec=$DurationSec periods=$Periods universe=$Universe limit=$MaxRepairCodes codes_len=$($Codes.Length) report=$FullPushReport"
    $FullPushArgs = @(
      $FullPushAggregatorScript,
      "--markets", "SH,SZ,BJ",
      "--universe", $Universe,
      "--periods", $Periods,
      "--duration-sec", "$DurationSec",
      "--flush-interval-sec", "60",
      "--poll-full-tick",
      "--write-daily",
      "--connect-retry-sec", "30",
      "--connect-deadline", $ActiveEnd,
      "--report", $FullPushReport
    )
    if ($MaxRepairCodes -gt 0) {
      $FullPushArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $FullPushArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $FullPushArgs += @("--include-index")
    }
    if ($FullPushDryRun) {
      $FullPushArgs += @("--dry-run")
    }

    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $FullPushArgs
    } finally {
      Pop-Location
    }
    Write-CollectorLog "END qmt_fullpush_intraday_aggregate exit_code=$ExitCode report=$FullPushReport"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }

  if ($Mode -eq "intraday-datadir-clickhouse") {
    $DatadirReport = Join-Path $StableReportDir ("intraday_datadir_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $IntradayBatchSize = [Math]::Max(1, $RepairCodeChunkSize)
    Write-CollectorLog "START qmt_datadir_intraday_clickhouse date=$StartDate~$EndDate periods=$Periods universe=$Universe limit=$MaxRepairCodes offset=$AppliedRepairCodeOffset batch_size=$IntradayBatchSize codes_len=$($Codes.Length)"
    $DatadirArgs = @(
      $DatadirIntradayScript,
      "--start-date", $StartDate,
      "--end-date", $EndDate,
      "--periods", $Periods,
      "--batch-size", "100",
      "--code-offset", "$AppliedRepairCodeOffset",
      "--report", $DatadirReport
    )
    if ($MaxRepairCodes -gt 0) {
      $DatadirArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $DatadirArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $DatadirArgs += @("--include-index")
    }

    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $DatadirArgs
    } finally {
      Pop-Location
    }

    $SelectedCodes = $null
    try {
      if (Test-Path -LiteralPath $DatadirReport) {
        $DatadirSummary = Get-Content -LiteralPath $DatadirReport -Raw | ConvertFrom-Json
        if ($null -ne $DatadirSummary.selected_codes) {
          $SelectedCodes = [int]$DatadirSummary.selected_codes
        }
      }
    } catch {
      $SelectedCodes = $null
    }
    if ($MaxRepairCodes -gt 0 -and $ExitCode -eq 0 -and $RollingStateFile) {
      if ($null -ne $SelectedCodes -and [int]$SelectedCodes -lt $MaxRepairCodes) {
        Write-RollingState -NextOffset 0
        Write-CollectorLog "ROLLING qmt_datadir_intraday_clickhouse reached universe tail selected_codes=$SelectedCodes; next_offset=0"
      } else {
        $NextOffset = $AppliedRepairCodeOffset + $MaxRepairCodes
        Write-RollingState -NextOffset $NextOffset
        Write-CollectorLog "ROLLING qmt_datadir_intraday_clickhouse next_offset=$NextOffset"
      }
    }
    Write-CollectorLog "END qmt_datadir_intraday_clickhouse exit_code=$ExitCode selected_codes=$SelectedCodes report=$DatadirReport"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }

  if ($Mode -eq "intraday-daily-minutes") {
    $IntradayDailyReport = Join-Path $StableReportDir ("intraday_daily_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $IntradayMinuteFetchReport = Join-Path $StableReportDir ("intraday_minutes_fetch_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $IntradayMinuteApplyReport = Join-Path $StableReportDir ("intraday_minutes_apply_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $IntradayBatchSize = [Math]::Max(1, $RepairCodeChunkSize)
    Write-CollectorLog "START qmt_xtquant_intraday_daily_minutes date=$StartDate~$EndDate periods=$Periods universe=$Universe limit=$MaxRepairCodes offset=$AppliedRepairCodeOffset batch_size=$IntradayBatchSize codes_len=$($Codes.Length)"

    $DailyArgs = @(
      $DailyBackfillScript,
      "--phase", "all",
      "--start-date", $StartDate,
      "--end-date", $EndDate,
      "--batch-size", "80",
      "--max-retries", "1",
      "--retry-sleep", "1",
      "--code-offset", "$AppliedRepairCodeOffset",
      "--skip-download",
      "--reset-stage",
      "--report", $IntradayDailyReport
    )
    if ($MaxRepairCodes -gt 0) {
      $DailyArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $DailyArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $DailyArgs += @("--include-index")
    }

    $MinuteCommonArgs = @(
      $MinuteBackfillScript,
      "--start-date", $StartDate,
      "--end-date", $EndDate,
      "--periods", $Periods,
      "--batch-size", "$IntradayBatchSize",
      "--batch-timeout-sec", "$MinuteBatchTimeoutSec",
      "--max-retries", "1",
      "--retry-sleep", "1",
      "--skip-download",
      "--code-offset", "$AppliedRepairCodeOffset"
    )
    if ($MaxRepairCodes -gt 0) {
      $MinuteCommonArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $MinuteCommonArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $MinuteCommonArgs += @("--include-index")
    }
    $MinuteFetchArgs = @($MinuteCommonArgs + @("--phase", "fetch", "--reset-stage", "--reset-stage-codes-only", "--report", $IntradayMinuteFetchReport))
    $MinuteApplyArgs = @($MinuteCommonArgs + @("--phase", "apply", "--no-require-complete-5m-for-apply", "--report", $IntradayMinuteApplyReport))

    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $DailyArgs
      if ($ExitCode -eq 0) {
        $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $MinuteFetchArgs
      }
      if ($ExitCode -eq 0) {
        $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $MinuteApplyArgs
      }
    } finally {
      Pop-Location
    }

    $RefreshResult = Get-IntradayRefreshResult -Path $IntradayMinuteFetchReport
    if ($ExitCode -eq 0 -and $null -ne $RefreshResult.FailedBatches -and [int]$RefreshResult.FailedBatches -gt 0) {
      $ExitCode = 2
    }
    if ($MaxRepairCodes -gt 0 -and $ExitCode -eq 0 -and $RollingStateFile) {
      $SelectedCodes = $RefreshResult.SelectedCodes
      if ($null -ne $SelectedCodes -and [int]$SelectedCodes -lt $MaxRepairCodes) {
        Write-RollingState -NextOffset 0
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_daily_minutes reached universe tail selected_codes=$SelectedCodes; next_offset=0"
      } else {
        $NextOffset = $AppliedRepairCodeOffset + $MaxRepairCodes
        Write-RollingState -NextOffset $NextOffset
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_daily_minutes next_offset=$NextOffset"
      }
    }
    Write-CollectorLog "END qmt_xtquant_intraday_daily_minutes exit_code=$ExitCode selected_codes=$($RefreshResult.SelectedCodes) failed_batches=$($RefreshResult.FailedBatches) daily_report=$IntradayDailyReport minute_fetch_report=$IntradayMinuteFetchReport minute_apply_report=$IntradayMinuteApplyReport"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }

  if ($Mode -eq "intraday-latest-5m") {
    $LatestFetchReport = Join-Path $StableReportDir ("intraday_latest_5m_fetch_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $LatestApplyReport = Join-Path $StableReportDir ("intraday_latest_5m_apply_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $IntradayBatchSize = [Math]::Max(1, $RepairCodeChunkSize)
    Write-CollectorLog "START qmt_xtquant_intraday_latest_5m date=$StartDate~$EndDate universe=$Universe limit=$MaxRepairCodes offset=$AppliedRepairCodeOffset batch_size=$IntradayBatchSize codes_len=$($Codes.Length)"
    $CommonArgs = @(
      $MinuteBackfillScript,
      "--start-date", $StartDate,
      "--end-date", $EndDate,
      "--periods", "5m",
      "--batch-size", "$IntradayBatchSize",
      "--batch-timeout-sec", "$MinuteBatchTimeoutSec",
      "--max-retries", "1",
      "--retry-sleep", "1",
      "--code-offset", "$AppliedRepairCodeOffset"
    )
    if ($MaxRepairCodes -gt 0) {
      $CommonArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $CommonArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $CommonArgs += @("--include-index")
    }
    $FetchArgs = @($CommonArgs + @("--phase", "fetch", "--reset-stage", "--reset-stage-codes-only", "--report", $LatestFetchReport))
    $ApplyArgs = @($CommonArgs + @("--phase", "apply-latest-5m", "--report", $LatestApplyReport))

    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $FetchArgs
      if ($ExitCode -eq 0) {
        $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $ApplyArgs
      }
    } finally {
      Pop-Location
    }

    $RefreshResult = Get-IntradayRefreshResult -Path $LatestFetchReport
    if ($ExitCode -eq 0 -and $null -ne $RefreshResult.FailedBatches -and [int]$RefreshResult.FailedBatches -gt 0) {
      $ExitCode = 2
    }
    if ($MaxRepairCodes -gt 0 -and $ExitCode -eq 0 -and $RollingStateFile) {
      $SelectedCodes = $RefreshResult.SelectedCodes
      if ($null -ne $SelectedCodes -and [int]$SelectedCodes -lt $MaxRepairCodes) {
        Write-RollingState -NextOffset 0
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_latest_5m reached universe tail selected_codes=$SelectedCodes; next_offset=0"
      } else {
        $NextOffset = $AppliedRepairCodeOffset + $MaxRepairCodes
        Write-RollingState -NextOffset $NextOffset
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_latest_5m next_offset=$NextOffset"
      }
    }
    Write-CollectorLog "END qmt_xtquant_intraday_latest_5m exit_code=$ExitCode selected_codes=$($RefreshResult.SelectedCodes) failed_batches=$($RefreshResult.FailedBatches) fetch_report=$LatestFetchReport apply_report=$LatestApplyReport"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }

  if ($Mode -eq "intraday-refresh") {
    $RefreshFetchReport = Join-Path $StableReportDir ("intraday_refresh_fetch_offset_{0}.json" -f $AppliedRepairCodeOffset)
    $RefreshApplyReport = Join-Path $StableReportDir ("intraday_refresh_apply_offset_{0}.json" -f $AppliedRepairCodeOffset)
    Write-CollectorLog "START qmt_xtquant_intraday_refresh date=$StartDate~$EndDate periods=$Periods universe=$Universe limit=$MaxRepairCodes offset=$AppliedRepairCodeOffset codes_len=$($Codes.Length)"
    $CommonArgs = @(
      $MinuteBackfillScript,
      "--start-date", $StartDate,
      "--end-date", $EndDate,
      "--periods", $Periods,
      "--batch-size", "1",
      "--batch-timeout-sec", "$MinuteBatchTimeoutSec",
      "--max-retries", "1",
      "--retry-sleep", "1",
      "--code-offset", "$AppliedRepairCodeOffset"
    )
    if ($MaxRepairCodes -gt 0) {
      $CommonArgs += @("--limit", "$MaxRepairCodes")
    }
    if ($Codes) {
      $CommonArgs += @("--codes", $Codes)
    }
    if ($Universe -match "index") {
      $CommonArgs += @("--include-index")
    }
    $FetchArgs = @($CommonArgs + @("--phase", "fetch", "--reset-stage", "--reset-stage-codes-only", "--report", $RefreshFetchReport))
    $ApplyArgs = @($CommonArgs + @("--phase", "apply", "--no-require-complete-5m-for-apply", "--report", $RefreshApplyReport))

    Push-Location $RootDir
    try {
      $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $FetchArgs
      if ($ExitCode -eq 0) {
        $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $ApplyArgs
      }
    } finally {
      Pop-Location
    }

    $RefreshResult = Get-IntradayRefreshResult -Path $RefreshFetchReport
    if ($ExitCode -eq 0 -and $null -ne $RefreshResult.FailedBatches -and [int]$RefreshResult.FailedBatches -gt 0) {
      $ExitCode = 2
    }
    if ($MaxRepairCodes -gt 0 -and $ExitCode -eq 0 -and $RollingStateFile) {
      $SelectedCodes = $RefreshResult.SelectedCodes
      if ($null -ne $SelectedCodes -and [int]$SelectedCodes -lt $MaxRepairCodes) {
        Write-RollingState -NextOffset 0
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_refresh reached universe tail selected_codes=$SelectedCodes; next_offset=0"
      } else {
        $NextOffset = $AppliedRepairCodeOffset + $MaxRepairCodes
        Write-RollingState -NextOffset $NextOffset
        Write-CollectorLog "ROLLING qmt_xtquant_intraday_refresh next_offset=$NextOffset"
      }
    }
    Write-CollectorLog "END qmt_xtquant_intraday_refresh exit_code=$ExitCode selected_codes=$($RefreshResult.SelectedCodes) failed_batches=$($RefreshResult.FailedBatches) fetch_report=$RefreshFetchReport apply_report=$RefreshApplyReport"
    return [PSCustomObject]@{
      ExitCode = $ExitCode
      WorkerIssueCount = $null
    }
  }

  $CollectorMode = $Mode
  $FinalizeMarker = Join-Path $StableReportDir "after_close_finalize_done.json"
  if ($Mode -eq "after-close-finalize") {
    $ExistingValidation = Join-Path $StableReportDir "final_validation.json"
    if (Test-Path -LiteralPath $ExistingValidation) {
      try {
        $Validation = Get-Content -LiteralPath $ExistingValidation -Raw | ConvertFrom-Json
        if ($Validation.closed -eq $true) {
          Write-CollectorLog "SKIP after_close_finalize already_closed validation=$ExistingValidation"
          return [PSCustomObject]@{
            ExitCode = 0
            WorkerIssueCount = 0
          }
        }
      } catch {
        Write-CollectorLog "WARN after_close_finalize unreadable existing validation=$ExistingValidation error=$($_.Exception.Message)"
      }
    }
    # After close, do not try to synthesize a completed day from new ticks.
    # Refresh QMT's 1d/5m local cache in batches, read it back in batches, and
    # perform a full final audit.  The intraday whole-quote path is unchanged.
    $CollectorMode = "after-close-full-refresh"
  }

  # `issues.csv` is only a resumable work queue when it contains at least one
  # code. A successful audit may leave an empty CSV behind; forwarding that
  # file to pandas on the next midnight run raises EmptyDataError and turns a
  # closed validation into a false task failure.
  $IssueCodeCount = Get-IssueCodeCount -Path $StableIssueFile
  if ((Test-Path -LiteralPath $StableIssueFile) -and $IssueCodeCount -eq 0) {
    Remove-Item -LiteralPath $StableIssueFile -Force
    Write-CollectorLog "RESET qmt_xtquant_collector empty_or_unreadable_issue_file=$StableIssueFile; will run a fresh audit"
  }
  if ($MaxRepairCodes -gt 0 -and $IssueCodeCount -gt 0 -and $AppliedRepairCodeOffset -ge $IssueCodeCount) {
    Remove-Item -LiteralPath $StableIssueFile -Force
    Write-RollingState -NextOffset 0
    $AppliedRepairCodeOffset = 0
    Write-CollectorLog "ROLLING qmt_xtquant_collector exhausted cached issue file; removed issue_file=$StableIssueFile for fresh audit"
  }

  Write-CollectorLog "START qmt_xtquant_collector mode=$CollectorMode scenario=$Scenario date=$StartDate~$EndDate periods=$Periods universe=$Universe codes_len=$($Codes.Length)"

  $Args = @(
    $CollectorScript,
    "--mode", $CollectorMode,
    "--scenario", $Scenario,
    "--start-date", $StartDate,
    "--end-date", $EndDate,
    "--minute-periods", $Periods,
    "--universe", $Universe,
    "--minute-batch-size", "$MinuteBatchSize",
    "--repair-code-chunk-size", "$RepairCodeChunkSize",
    "--repair-workers", "$RepairWorkers",
    "--minute-batch-timeout-sec", "$MinuteBatchTimeoutSec",
    "--minute-chunk-timeout-sec", "$MinuteChunkTimeoutSec",
    "--minute-timeout-sec", "$MinuteTimeoutSec",
    "--daily-batch-size", "80",
    "--max-retries", "1",
    "--retry-sleep", "1",
    "--minute-report-dir", $StableReportDir
  )

  if ($Codes) {
    $Args += @("--codes", $Codes)
  }
  if ($CollectorMode -eq "minute-gap-repair" -and (Test-Path -LiteralPath $StableIssueFile) -and $IssueCodeCount -gt 0) {
    $Args += @("--issue-file", $StableIssueFile)
    Write-CollectorLog "REUSE qmt_xtquant_collector issue_file=$StableIssueFile"
  }
  if ($CollectorMode -eq "minute-gap-repair" -and $MaxRepairCodes -gt 0) {
    $Args += @("--max-repair-codes", "$MaxRepairCodes", "--repair-code-offset", "$AppliedRepairCodeOffset")
    Write-CollectorLog "ROLLING qmt_xtquant_collector max_repair_codes=$MaxRepairCodes repair_code_offset=$AppliedRepairCodeOffset state=$RollingStateFile"
  }

  if ($Scenario -eq "history" -and $CollectorMode -eq "minute-gap-repair") {
    $Args += "--isolate-history-periods"
    Write-CollectorLog "ISOLATE historical periods; previous-close delivery remains independently monitored"
  }
  Push-Location $RootDir
  try {
    $ExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $Args
  } finally {
    Pop-Location
  }

  if ($Scenario -ne "history" -and $CollectorMode -eq "minute-gap-repair" -and $MaxRepairCodes -gt 0 -and $ExitCode -eq 0 -and $RollingStateFile) {
    $NextOffset = $AppliedRepairCodeOffset + $MaxRepairCodes
    $CurrentIssueCodeCount = Get-IssueCodeCount -Path $StableIssueFile
    if ($CurrentIssueCodeCount -gt 0 -and $NextOffset -ge $CurrentIssueCodeCount) {
      Write-RollingState -NextOffset 0
      Remove-Item -LiteralPath $StableIssueFile -Force
      Write-CollectorLog "ROLLING qmt_xtquant_collector completed cached issue list issue_codes=$CurrentIssueCodeCount; removed issue_file for fresh audit"
    } else {
      Write-RollingState -NextOffset $NextOffset
      Write-CollectorLog "ROLLING qmt_xtquant_collector next_offset=$NextOffset"
    }
  }

  $WorkerIssueCount = Get-WorkerIssueCount -Path (Join-Path $StableReportDir "collector_summary.json")
  if ($Mode -eq "after-close-finalize" -and $ExitCode -eq 0 -and $null -ne $WorkerIssueCount -and [int]$WorkerIssueCount -eq 0) {
    if (-not (Test-Path -LiteralPath $FinalizeMarker)) {
      $GovernanceOutDir = Join-Path $StableReportDir "governance"
      Write-CollectorLog "START after_close_governance date=$StartDate~$EndDate out_dir=$GovernanceOutDir"
      $GovernanceArgs = @(
        $GovernanceScript,
        "--start-date", $StartDate,
        "--end-date", $EndDate,
        "--periods", "5m,15m,30m,60m,1d",
        "--use-final",
        "--check-derived",
        "--repair-bad-ohlc",
        "--rebuild-derived",
        "--optimize-final",
        "--out-dir", $GovernanceOutDir
      )
      Push-Location $RootDir
      try {
        $GovernanceExitCode = Invoke-LoggedNative -Exe $PythonExe -Arguments $GovernanceArgs
      } finally {
        Pop-Location
      }
      Write-CollectorLog "END after_close_governance exit_code=$GovernanceExitCode out_dir=$GovernanceOutDir"
      if ($GovernanceExitCode -ne 0) {
        return [PSCustomObject]@{
          ExitCode = $GovernanceExitCode
          WorkerIssueCount = $WorkerIssueCount
        }
      }
      @{
        date_key = $DateKey
        done_at = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        out_dir = $GovernanceOutDir
      } | ConvertTo-Json | Out-File -LiteralPath $FinalizeMarker -Encoding utf8
    } else {
      Write-CollectorLog "SKIP after_close_governance marker_exists=$FinalizeMarker"
    }
  }
  if ($CollectorMode -eq "minute-gap-repair" -and $RetryAfterCloseSourceEmpty) {
    $Args += "--retry-after-close-source-empty"
    Write-CollectorLog "ENABLE qmt_xtquant_collector retry_after_close_source_empty"
  }
  Write-CollectorLog "END qmt_xtquant_collector exit_code=$ExitCode worker_issue_count=$WorkerIssueCount"
  return [PSCustomObject]@{
    ExitCode = $ExitCode
    WorkerIssueCount = $WorkerIssueCount
  }
}

function Test-DailyCoverageNoProgress {
  if ($Mode -ne "daily-coverage-repair") {
    return $false
  }
  $ReportPath = "F:\Stock\AiStockData\data\runtime\daily_kline_coverage\latest.json"
  try {
    $Report = Get-Content -LiteralPath $ReportPath -Raw -Encoding utf8 | ConvertFrom-Json
    return [bool]$Report.repair.stop_continuous
  } catch {
    Write-CollectorLog "WARN qmt_daily_coverage_repair unable_to_read_stop_signal error=$($_.Exception.Message)"
    return $false
  }
}

do {
  $Result = Invoke-CollectorOnce
  if ($Result.ExitCode -ne 0) {
    exit $Result.ExitCode
  }
  if (Test-DailyCoverageNoProgress) {
    Write-CollectorLog "STOP qmt_daily_coverage_repair no_progress_after_full_cycle"
    if ($DisableTaskOnNoProgress) {
      try {
        Disable-ScheduledTask -TaskName $DisableTaskOnNoProgress -ErrorAction Stop | Out-Null
        Write-CollectorLog "DISABLE qmt_daily_coverage_repair task=$DisableTaskOnNoProgress reason=no_progress_after_full_cycle"
      } catch {
        Write-CollectorLog "ERROR qmt_daily_coverage_repair disable_task_failed task=$DisableTaskOnNoProgress error=$($_.Exception.Message)"
        exit 1
      }
    }
    exit 0
  }
  if ($null -ne $Result.WorkerIssueCount -and [int]$Result.WorkerIssueCount -eq 0) {
    Write-CollectorLog "COMPLETE qmt_xtquant_collector no minute gap issues remain"
    exit 0
  }
  if (-not $ContinuousUntilWindowEnd) {
    exit $Result.ExitCode
  }
  if (-not $SkipTimeWindowCheck) {
    $Window = Test-ActiveWindow -Now (Get-Date)
    if (-not $Window.Ok) {
      Write-CollectorLog "STOP qmt_xtquant_collector continuous loop $($Window.Reason)"
      exit 0
    }
  }
  Start-Sleep -Seconds ([Math]::Max(0, $ContinuousSleepSeconds))
} while ($true)
