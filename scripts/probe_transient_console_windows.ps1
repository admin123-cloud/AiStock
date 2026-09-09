param(
  [int]$DurationSeconds = 600,
  [string]$LogPath = "F:\Stock\AiStockData\data\runtime\transient_console_window_probe_background.jsonl"
)

$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $LogPath
if ($dir) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$seen = @{}
$interesting = "powershell.exe|pwsh.exe|cmd.exe|conhost.exe|WindowsTerminal.exe|wt.exe|wscript.exe|cscript.exe"
$end = (Get-Date).AddSeconds($DurationSeconds)

while ((Get-Date) -lt $end) {
  $procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match $interesting }
  foreach ($p in $procs) {
    $key = [string]$p.ProcessId
    if ($seen.ContainsKey($key)) {
      continue
    }
    $seen[$key] = $true
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)"
    $grand = $null
    if ($parent) {
      $grand = Get-CimInstance Win32_Process -Filter "ProcessId = $($parent.ParentProcessId)"
    }
    $item = [ordered]@{
      captured_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
      pid = $p.ProcessId
      name = $p.Name
      parent_pid = $p.ParentProcessId
      command_line = $p.CommandLine
      creation_date = if ($p.CreationDate) { $p.CreationDate.ToString("yyyy-MM-dd HH:mm:ss.fff") } else { $null }
      parent_name = if ($parent) { $parent.Name } else { $null }
      parent_command_line = if ($parent) { $parent.CommandLine } else { $null }
      grandparent_pid = if ($grand) { $grand.ProcessId } else { $null }
      grandparent_name = if ($grand) { $grand.Name } else { $null }
      grandparent_command_line = if ($grand) { $grand.CommandLine } else { $null }
    }
    ($item | ConvertTo-Json -Compress -Depth 4) | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }
  Start-Sleep -Milliseconds 200
}
