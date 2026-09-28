param(
  [ValidateSet('15','30')][string]$Period = '30',
  [Parameter(Mandatory=$true)][string]$RunId
)
$ErrorActionPreference = 'Stop'
$manifest = "F:\AiStock\data\data\runtime\minute_migration_v2\baseline_${RunId}.json"
if (-not (Test-Path -LiteralPath $manifest)) { throw "baseline manifest not found: $manifest" }
$months = (Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json).months.month
foreach ($month in $months) {
  & python scripts/minute_table_migration_v2.py build-month --period $Period --month "$month" --run-id $RunId --drop-existing-partition
  if ($LASTEXITCODE -ne 0) { throw "copy failed for $month with exit code $LASTEXITCODE" }
}
