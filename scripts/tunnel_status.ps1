$runtimeDir = "$env:USERPROFILE\cloudflared\runtime"
$pidFile = Join-Path $runtimeDir "public_tunnel.pid"
$logFile = Join-Path $runtimeDir "public_tunnel.log"

$tunnelPid = $null
$running = $false
$publicUrl = $null

if (Test-Path $pidFile) {
  $tunnelPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($tunnelPid) {
    $proc = Get-Process -Id ([int]$tunnelPid) -ErrorAction SilentlyContinue
    $running = [bool]$proc
  }
}

if (Test-Path $logFile) {
  $match = Select-String -Path $logFile -Pattern "https://[a-zA-Z0-9.-]+trycloudflare\.com" -AllMatches -ErrorAction SilentlyContinue | Select-Object -Last 1
  if ($match) {
    $m = [regex]::Match($match.Line, "https://[a-zA-Z0-9.-]+trycloudflare\.com")
    if ($m.Success) { $publicUrl = $m.Value }
  }
}

[pscustomobject]@{
  Running = $running
  Pid = $tunnelPid
  PublicUrl = $publicUrl
  LogFile = $logFile
}
