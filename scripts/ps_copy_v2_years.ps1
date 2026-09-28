param(
  [ValidateSet('15','30')][string]$Period = '15',
  [Parameter(Mandatory=$true)][string]$RunId
)
$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\ps_copy_v2_months.ps1" -Period $Period -RunId $RunId
if ($LASTEXITCODE -ne 0) { throw "monthly copy failed with exit code $LASTEXITCODE" }
