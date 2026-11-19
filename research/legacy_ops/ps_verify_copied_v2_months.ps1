param(
  [Parameter(Mandatory=$true)][ValidateSet('15','30')][string]$Period,
  [Parameter(Mandatory=$true)][string]$RunId
)
$ErrorActionPreference = 'Stop'
$manifest = "F:\AiStock\data\data\runtime\minute_migration_v2\baseline_${RunId}.json"
if (-not (Test-Path -LiteralPath $manifest)) { throw "baseline manifest not found: $manifest" }
$months = (Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json).months.month
foreach ($month in $months) {
  & python scripts/minute_table_migration_v2.py verify-month --period $Period --month "$month" --run-id $RunId
  if ($LASTEXITCODE -ne 0) { throw "verify failed for $month with exit code $LASTEXITCODE" }
}
