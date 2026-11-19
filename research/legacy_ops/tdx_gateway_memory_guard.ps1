param(
  [double]$ThresholdGB = 8,
  [int]$Port = 8765,
  [switch]$EnsureRunning,
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
  $LogFile = Join-Path $LogDir "tdx_gateway_memory_guard.log"
}

function Write-GuardLog {
  param([string]$Message)
  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -LiteralPath $LogFile -Value "[$stamp] $Message" -Encoding UTF8
}

function Get-GatewayProcesses {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
      $cmd = [string]$_.CommandLine
      $cmd -match "uvicorn\s+services\.tdx_gateway:app" -and
      $cmd -match "--port\s+$Port(\s|$)"
    }
}

function Test-GatewayHealthy {
  try {
    $health = Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$Port/health"
    return [bool]($health.ready -eq $true -or [string]$health.status -eq "available")
  } catch {
    return $false
  }
}

function Start-Gateway {
  if (-not (Test-Path -LiteralPath $StartScript)) {
    Write-GuardLog "start script missing: $StartScript"
    return
  }
  Write-GuardLog "starting TDX Gateway via $StartScript"
  Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$StartScript`"" -WorkingDirectory $RootDir -WindowStyle Hidden | Out-Null
}

$thresholdBytes = [int64]($ThresholdGB * 1GB)
$processes = @(Get-GatewayProcesses)

foreach ($process in $processes) {
  $pidValue = [int]$process.ProcessId
  $privateBytes = [int64]$process.PageFileUsage * 1KB
  $virtualBytes = [int64]$process.VirtualSize
  $workingSetBytes = [int64]$process.WorkingSetSize

  if ($privateBytes -ge $thresholdBytes) {
    Write-GuardLog (
      "threshold exceeded; pid=$pidValue privateGB={0:N2} virtualGB={1:N2} wsGB={2:N2} thresholdGB={3:N2}" -f
      ($privateBytes / 1GB), ($virtualBytes / 1GB), ($workingSetBytes / 1GB), $ThresholdGB
    )
    try {
      Stop-Process -Id $pidValue -Force -ErrorAction Stop
      Start-Sleep -Seconds 5
      Write-GuardLog "stopped pid=$pidValue"
    } catch {
      Write-GuardLog "failed to stop pid=$pidValue error=$($_.Exception.Message)"
    }
  }
}

$processesAfter = @(Get-GatewayProcesses)
if ($EnsureRunning -and $processesAfter.Count -eq 0) {
  Start-Gateway
  Start-Sleep -Seconds 5
  if (Test-GatewayHealthy) {
    Write-GuardLog "TDX Gateway restarted and health check passed"
  } else {
    Write-GuardLog "TDX Gateway start requested; health check not ready yet"
  }
}
