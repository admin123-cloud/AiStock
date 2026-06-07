param(
  [string]$LocalUrl = "http://localhost:3000"
)

$ErrorActionPreference = "Stop"

$cloudflared = "$env:USERPROFILE\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
  throw "cloudflared not found: $cloudflared"
}

$runtimeDir = "$env:USERPROFILE\cloudflared\runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

$pidFile = Join-Path $runtimeDir "public_tunnel.pid"
$logFile = Join-Path $runtimeDir "public_tunnel.log"

if (Test-Path $pidFile) {
  $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($oldPid) {
    $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
    if ($proc) {
      Write-Output "Tunnel is already running. PID=$($proc.Id)"
      Write-Output "Log: $logFile"
      exit 0
    }
  }
}

if (Test-Path $logFile) {
  Remove-Item $logFile -Force -ErrorAction SilentlyContinue
}

$args = @(
  "tunnel",
  "--url", $LocalUrl,
  "--no-autoupdate",
  "--protocol", "http2",
  "--loglevel", "info",
  "--logfile", $logFile
)

$proc = Start-Process -FilePath $cloudflared -ArgumentList $args -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $proc.Id -NoNewline

$publicUrl = $null
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  if (-not (Test-Path $logFile)) { continue }
  $match = Select-String -Path $logFile -Pattern "https://[a-zA-Z0-9.-]+trycloudflare\.com" -AllMatches -ErrorAction SilentlyContinue | Select-Object -Last 1
  if ($match) {
    $m = [regex]::Match($match.Line, "https://[a-zA-Z0-9.-]+trycloudflare\.com")
    if ($m.Success) {
      $publicUrl = $m.Value
      break
    }
  }
}

Write-Output "Tunnel started. PID=$($proc.Id)"
Write-Output "Log: $logFile"
if ($publicUrl) {
  Write-Output "Public URL: $publicUrl"
} else {
  Write-Output "Public URL not found yet. Run scripts/tunnel_status.ps1 to check."
}
