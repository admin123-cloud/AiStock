$ErrorActionPreference = "Stop"

$runtimeDir = "$env:USERPROFILE\cloudflared\runtime"
$pidFile = Join-Path $runtimeDir "public_tunnel.pid"

if (-not (Test-Path $pidFile)) {
  Write-Output "No running tunnel pid file found."
  exit 0
}

$tunnelPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $tunnelPid) {
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
  Write-Output "Stale pid file removed."
  exit 0
}

$proc = Get-Process -Id ([int]$tunnelPid) -ErrorAction SilentlyContinue
if ($proc) {
  Stop-Process -Id $proc.Id -Force
  Write-Output "Stopped tunnel process. PID=$($proc.Id)"
} else {
  Write-Output "Tunnel process already stopped."
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
