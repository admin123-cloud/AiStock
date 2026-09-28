param(
  [double]$ThresholdGB = 16,
  [int]$Port = 8765,
  [int]$IntervalSeconds = 30,
  [switch]$EnsureRunning,
  [int]$RestartCooldownSeconds = 90,
  [string]$StartScript = "",
  [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (-not $StartScript) {
  $StartScript = Join-Path $RootDir "scripts\start_tdx_gateway.bat"
}
if (-not $LogFile) {
  $LogDir = Join-Path $RootDir "runtime\logs"
  if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
  }
  $LogFile = Join-Path $LogDir "tdx_gateway_memory_watchdog.csv"
}

function Write-Sample {
  param([string]$Line)
  Add-Content -LiteralPath $LogFile -Value $Line -Encoding UTF8
}

function Get-GatewayProcesses {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
      $cmd = [string]$_.CommandLine
      $cmd -match "uvicorn\s+services\.tdx_gateway:app" -and
      $cmd -match "--port\s+$Port(\s|$)"
    }
}

if (-not (Test-Path -LiteralPath $LogFile)) {
  Write-Sample "timestamp,pid,private_gb,working_set_gb,virtual_gb,free_virtual_gb,threshold_gb,action"
}

$thresholdBytes = [int64]($ThresholdGB * 1GB)
$lastRestartAt = [datetime]::MinValue

function Start-Gateway {
  param([string]$Reason)
  if (-not (Test-Path -LiteralPath $StartScript)) {
    Write-Sample "$(Get-Date -Format "yyyy-MM-dd HH:mm:ss"),,0,0,0,,$ThresholdGB,start_script_missing:$StartScript"
    return
  }
  $elapsed = ([datetime]::Now - $script:lastRestartAt).TotalSeconds
  if ($elapsed -lt $RestartCooldownSeconds) {
    Write-Sample "$(Get-Date -Format "yyyy-MM-dd HH:mm:ss"),,0,0,0,,$ThresholdGB,restart_cooldown:$Reason"
    return
  }
  $script:lastRestartAt = [datetime]::Now
  try {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$StartScript`"" -WorkingDirectory $RootDir -WindowStyle Hidden | Out-Null
    Write-Sample "$(Get-Date -Format "yyyy-MM-dd HH:mm:ss"),,0,0,0,,$ThresholdGB,restart_requested:$Reason"
  } catch {
    $message = $_.Exception.Message -replace ',', ';'
    Write-Sample "$(Get-Date -Format "yyyy-MM-dd HH:mm:ss"),,0,0,0,,$ThresholdGB,restart_failed:$message"
  }
}

while ($true) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    $freeVirtualGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
    $processes = @(Get-GatewayProcesses)

    if ($processes.Count -eq 0) {
      Write-Sample "$stamp,,0,0,0,$freeVirtualGB,$ThresholdGB,no_gateway"
      if ($EnsureRunning) {
        Start-Gateway -Reason "no_gateway"
      }
    }

    foreach ($process in $processes) {
      $pidValue = [int]$process.ProcessId
      $privateBytes = [int64]$process.PageFileUsage * 1KB
      $workingSetBytes = [int64]$process.WorkingSetSize
      $virtualBytes = [int64]$process.VirtualSize
      $privateGB = [math]::Round($privateBytes / 1GB, 3)
      $workingSetGB = [math]::Round($workingSetBytes / 1GB, 3)
      $virtualGB = [math]::Round($virtualBytes / 1GB, 3)
      $action = "sample"

      if ($privateBytes -ge $thresholdBytes) {
        $action = "stop_threshold_exceeded"
        try {
          Stop-Process -Id $pidValue -Force -ErrorAction Stop
          if ($EnsureRunning) {
            Start-Sleep -Seconds 5
            Start-Gateway -Reason "threshold_exceeded"
          }
        } catch {
          $action = "stop_failed:$($_.Exception.Message -replace ',', ';')"
        }
      }

      Write-Sample "$stamp,$pidValue,$privateGB,$workingSetGB,$virtualGB,$freeVirtualGB,$ThresholdGB,$action"
    }
  } catch {
    $message = $_.Exception.Message -replace ',', ';'
    Write-Sample "$stamp,,,,,,${ThresholdGB},watchdog_error:$message"
  }

  Start-Sleep -Seconds $IntervalSeconds
}
