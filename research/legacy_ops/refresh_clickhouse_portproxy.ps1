$ErrorActionPreference = "SilentlyContinue"

netsh interface portproxy delete v4tov4 listenport=8123 listenaddress=127.0.0.1 | Out-Null

Write-Host "Deprecated script: portproxy disabled."
Write-Host "Use Docker ClickHouse only (aistock-clickhouse)."
Write-Host "Current portproxy table:"
netsh interface portproxy show v4tov4
