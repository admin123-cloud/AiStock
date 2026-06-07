$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $root "docker-compose.clickhouse.yml"
if (!(Test-Path $compose)) {
    throw "docker-compose.clickhouse.yml not found: $compose"
}

Write-Host "Starting Docker ClickHouse (single-instance mode)..."
docker compose -f $compose up -d clickhouse | Out-Host

$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8123/?query=SELECT%201" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200 -and ($resp.Content -match "1")) {
            Write-Host "ClickHouse is ready on 127.0.0.1:8123 (Docker)"
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 800
}

throw "ClickHouse did not become ready in time"
