param(
  [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"

$matches = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*ptrade_file_bridge_strategy.py*" }

$stopped = @()
foreach ($item in $matches) {
  if (-not $WhatIfOnly) {
    Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
  }
  $stopped += [pscustomobject]@{
    process_id = $item.ProcessId
    command_line = $item.CommandLine
    stopped = -not $WhatIfOnly
  }
}

[pscustomobject]@{
  ok = $true
  what_if = [bool]$WhatIfOnly
  matched_count = @($matches).Count
  stopped = $stopped
} | ConvertTo-Json -Depth 5
