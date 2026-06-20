param(
    [string]$BridgeDir = "F:\Stock\AiStockData\data\runtime\ptrade_bridge",
    [string]$PtradeApiDir = "D:\PTrade\ptrade\Libs\Python\api",
    [string]$PtradePython = "D:\PTrade\ptrade\Libs\Python\Libs\Python\python3\python.exe",
    [double]$PollSeconds = 2
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$runnerScript = Join-Path $repoRoot "scripts\ptrade_file_bridge_api_runner.py"
$logDir = "F:\Stock\AiStockData\logs\ptrade_bridge"
$outLog = Join-Path $logDir "runner.out.log"
$errLog = Join-Path $logDir "runner.err.log"

New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^python' -and
        $_.CommandLine -like '*ptrade_file_bridge_api_runner.py*' -and
        $_.CommandLine -like '*--loop*'
    } |
    Select-Object -First 1

if ($existing) {
    [pscustomobject]@{
        ok = $true
        status = "already_running"
        process_id = $existing.ProcessId
        bridge_dir = $BridgeDir
        runner_script = $runnerScript
    } | ConvertTo-Json -Depth 4
    exit 0
}

if (-not (Test-Path -LiteralPath $PtradePython)) {
    throw "PTrade Python not found: $PtradePython"
}
if (-not (Test-Path -LiteralPath $runnerScript)) {
    throw "Runner script not found: $runnerScript"
}

$args = @(
    $runnerScript,
    "--loop",
    "--bridge-dir", $BridgeDir,
    "--ptrade-api-dir", $PtradeApiDir,
    "--poll-seconds", [string]$PollSeconds
)

$process = Start-Process `
    -FilePath $PtradePython `
    -ArgumentList $args `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden `
    -PassThru

[pscustomobject]@{
    ok = $true
    status = "started"
    process_id = $process.Id
    bridge_dir = $BridgeDir
    runner_script = $runnerScript
    stdout_log = $outLog
    stderr_log = $errLog
} | ConvertTo-Json -Depth 4
