param(
  [string]$PythonExe = "D:\PTrade\ptrade\Libs\Python\Libs\Python\python3\python.exe",
  [string]$StrategyFile = "F:\Stock\AiStock-core\reports\ptrade_bridge_deploy_package\ptrade_file_bridge_strategy.py",
  [string]$PTradeApiDir = "D:\PTrade\ptrade\Libs\Python\api"
)

$ErrorActionPreference = "Stop"

$existing = Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -like "*ptrade_file_bridge_strategy.py*" -and
    $_.CommandLine -like "*$StrategyFile*"
  }

if ($existing) {
  $existing | Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Depth 4
  exit 0
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
  throw "PTrade Python not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $StrategyFile)) {
  throw "Strategy file not found: $StrategyFile"
}
if (-not (Test-Path -LiteralPath $PTradeApiDir)) {
  throw "PTrade API dir not found: $PTradeApiDir"
}

$env:PYTHONPATH = "$PTradeApiDir;$env:PYTHONPATH"
$process = Start-Process `
  -FilePath $PythonExe `
  -ArgumentList @($StrategyFile) `
  -PassThru `
  -WindowStyle Hidden

[pscustomobject]@{
  ok = $true
  process_id = $process.Id
  python = $PythonExe
  strategy = $StrategyFile
  ptrade_api_dir = $PTradeApiDir
} | ConvertTo-Json -Depth 4
