param(
    [ValidateSet('Inspect','Build','Apply','Rollback')][string]$Mode = 'Inspect',
    [Parameter(Mandatory=$true)][string]$Version,
    [Parameter(Mandatory=$true)][string]$SettingsFile,
    [string]$HostRepoRoot = (Split-Path $PSScriptRoot -Parent)
)
$ErrorActionPreference = 'Stop'
$taskDocker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $taskDocker) { $taskDocker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
if (-not (Test-Path -LiteralPath $taskDocker -PathType Leaf)) { throw 'Docker CLI not found' }
if ($Version -notmatch '^[a-f0-9]{7,40}$') { throw 'Version must be an exact Git commit hash' }
$taskRepo = (Resolve-Path -LiteralPath $HostRepoRoot).Path
$taskSettings = (Resolve-Path -LiteralPath $SettingsFile).Path
if (-not (Test-Path -LiteralPath $taskSettings -PathType Leaf)) { throw 'Private settings file missing' }
foreach ($taskPath in @('F:\Stock\AiStockData\data','F:\Stock\AiStockData\artifacts','F:\Stock\AiStockData\logs','F:\Stock\AiStockResearchArchive\reports','D:\TDX\vipdoc')) {
    if (-not (Test-Path -LiteralPath $taskPath -PathType Container)) { throw "Required runtime directory missing: $taskPath" }
}
$taskHead = (& git -C $taskRepo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $taskHead.StartsWith($Version)) { throw 'Host checkout differs from release version' }
if (& git -C $taskRepo status --porcelain) { throw 'Release checkout has uncommitted or untracked files' }
$env:AISTOCK_RELEASE_VERSION = $Version
$env:AISTOCK_HOST_REPO_ROOT = $taskRepo
$env:AISTOCK_SETTINGS_FILE = $taskSettings
$taskCompose = Join-Path $taskRepo 'docker-compose.release.yml'
& $taskDocker compose -f $taskCompose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Release configuration invalid' }
Write-Output "Release $Version / mode $Mode / host $taskRepo"
if ($Mode -eq 'Inspect') {
    Write-Output 'Preflight passed. No containers or host tasks changed. Verify task entrypoints and full-day acceptance before Apply.'
    exit 0
}
if ($Mode -eq 'Build') {
    & $taskDocker build --label "org.opencontainers.image.revision=$taskHead" -f (Join-Path $taskRepo 'Dockerfile.backend') -t "aistock-core-backend:$Version" $taskRepo
    if ($LASTEXITCODE -ne 0) { throw 'Backend image build failed' }
    & $taskDocker build --label "org.opencontainers.image.revision=$taskHead" -f (Join-Path $taskRepo 'frontend\Dockerfile.release') -t "aistock-core-frontend:$Version" (Join-Path $taskRepo 'frontend')
    if ($LASTEXITCODE -ne 0) { throw 'Frontend image build failed' }
    exit 0
}
foreach ($taskImage in @("aistock-core-backend:$Version","aistock-core-frontend:$Version")) {
    $taskRevision = & $taskDocker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' $taskImage
    if ($LASTEXITCODE -ne 0 -or $taskRevision.Trim() -ne $taskHead) { throw "Image revision mismatch: $taskImage" }
}
$taskRecordDir = 'F:\Stock\AiStockData\artifacts\releases'
New-Item -ItemType Directory -Force -Path $taskRecordDir | Out-Null
$taskRecord = Join-Path $taskRecordDir ("{0}-{1}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'),$Mode)
$taskPrevious = & $taskDocker compose -f $taskCompose images --format json
if ($LASTEXITCODE -ne 0) { throw 'Could not record previous container images' }
@{version=$taskHead; mode=$Mode; host_repo=$taskRepo; previous_images=$taskPrevious; started_at=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $taskRecord -Encoding UTF8
& $taskDocker compose -f $taskCompose up -d --no-build --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw "Release not healthy. Preserve data; use recorded prior release checkout and Rollback. Record: $taskRecord" }
Write-Output "Containers ready. Record: $taskRecord. Host task publisher must target this verified checkout; trading-day acceptance remains separate."
