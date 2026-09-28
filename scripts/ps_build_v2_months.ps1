param(
  [ValidateSet('15','30','60')][string]$Period = '60',
  [Parameter(Mandatory=$true)][string]$RunId,
  [switch]$From5m
)
$ErrorActionPreference = 'Stop'
$manifest = "F:\AiStock\data\data\runtime\minute_migration_v2\baseline_${RunId}.json"
if (-not (Test-Path -LiteralPath $manifest)) { throw "baseline manifest not found: $manifest" }
python scripts/minute_table_migration_v2.py create --period $Period
if ($LASTEXITCODE -ne 0) { throw "create failed with exit code $LASTEXITCODE" }
$months = (Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json).months.month
foreach ($month in $months) {
  $args = @('scripts/minute_table_migration_v2.py','build-month','--period',$Period,'--month',"$month",'--run-id',$RunId,'--drop-existing-partition')
  if ($From5m -or $Period -eq '60') { $args += '--from-5m' }
  & python @args
  if ($LASTEXITCODE -ne 0) { throw "build failed for $month with exit code $LASTEXITCODE" }
}
