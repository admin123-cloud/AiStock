param(
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"

$targets = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^python' -and
        $_.CommandLine -like '*ptrade_file_bridge_api_runner.py*' -and
        $_.CommandLine -like '*--loop*'
    }

$stopped = @()
foreach ($target in $targets) {
    $item = [pscustomobject]@{
        process_id = $target.ProcessId
        command_line = $target.CommandLine
        stopped = $false
    }
    if (-not $WhatIfOnly) {
        Stop-Process -Id $target.ProcessId -Force
        $item.stopped = $true
    }
    $stopped += $item
}

[pscustomobject]@{
    ok = $true
    what_if = [bool]$WhatIfOnly
    matched_count = @($targets).Count
    stopped = $stopped
} | ConvertTo-Json -Depth 5
