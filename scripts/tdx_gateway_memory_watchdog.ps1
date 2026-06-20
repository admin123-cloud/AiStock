param(
  [double]$ThresholdGB = 16,
  [int]$Port = 8765,
  [int]$IntervalSeconds = 30,
  [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
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

while ($true) {
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    $freeVirtualGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
    $processes = @(Get-GatewayProcesses)

    if ($processes.Count -eq 0) {
      Write-Sample "$stamp,,0,0,0,$freeVirtualGB,$ThresholdGB,no_gateway"
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
