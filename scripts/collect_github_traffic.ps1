# Collect GitHub traffic into CSV (GitHub only keeps ~14 days in the UI).
# Run daily (locally or via GitHub Actions) to build month / 6-month history.
#
# Usage:
#   .\scripts\collect_github_traffic.ps1
#   .\scripts\collect_github_traffic.ps1 -Owner protocorn -Repo clippy-vision
#   .\scripts\collect_github_traffic.ps1 -OutDir "$HOME\github-traffic\clippy-vision"
#
# CI note: traffic endpoints need a PAT (TRAFFIC_TOKEN). Default GITHUB_TOKEN gets 403.

param(
    [string]$Owner = "protocorn",
    [string]$Repo = "clippy-vision",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh CLI not found. Install GitHub CLI and run: gh auth login"
}

if (-not $OutDir) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $OutDir = Join-Path $repoRoot "stats\github-traffic"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Invoke-GhJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    $raw = gh api $Path
    if (-not $raw) { return $null }
    # Unary comma prevents PowerShell from unrolling JSON arrays on return.
    $parsed = $raw | ConvertFrom-Json
    return , $parsed
}

function As-Array {
    param($Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Array]) { return $Value }
    return @($Value)
}

function Get-JsonInt {
    param([Parameter(Mandatory = $true)]$Object, [Parameter(Mandatory = $true)][string]$Name)
    # Prefer NoteProperty so PowerShell's intrinsic .Count does not shadow JSON "count".
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return 0 }
    return [int]$prop.Value
}

function Upsert-CsvRows {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Header,
        [Parameter(Mandatory = $true)][object[]]$Rows,
        [Parameter(Mandatory = $true)][string[]]$KeyColumns
    )

    $existing = @()
    if (Test-Path $FilePath) {
        $existing = @(Import-Csv -Path $FilePath)
    }

    $keyed = @{}
    foreach ($row in $existing) {
        $key = ($KeyColumns | ForEach-Object { [string]$row.$_ }) -join "|"
        $keyed[$key] = $row
    }

    foreach ($row in $Rows) {
        $key = ($KeyColumns | ForEach-Object { [string]$row.$_ }) -join "|"
        $keyed[$key] = $row
    }

    $merged = @($keyed.Values) | Sort-Object -Property $KeyColumns
    $merged | Select-Object -Property $Header | Export-Csv -Path $FilePath -NoTypeInformation -Encoding UTF8
}

$collectedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$repoPath = "repos/$Owner/$Repo"

Write-Host "Collecting traffic for $Owner/$Repo ..."

$views = Invoke-GhJson "$repoPath/traffic/views"
$clones = Invoke-GhJson "$repoPath/traffic/clones"
$referrers = As-Array (Invoke-GhJson "$repoPath/traffic/popular/referrers")
$paths = As-Array (Invoke-GhJson "$repoPath/traffic/popular/paths")

# --- daily views (one row per calendar day GitHub returned) ---
$viewRows = @()
foreach ($day in (As-Array $views.views)) {
    $viewRows += [pscustomobject]@{
        date            = ([datetime]$day.timestamp).ToUniversalTime().ToString("yyyy-MM-dd")
        views           = (Get-JsonInt $day "count")
        unique_visitors = (Get-JsonInt $day "uniques")
        collected_at    = $collectedAt
    }
}
Upsert-CsvRows -FilePath (Join-Path $OutDir "views_daily.csv") `
    -Header @("date", "views", "unique_visitors", "collected_at") `
    -Rows $viewRows `
    -KeyColumns @("date")

# --- daily clones ---
$cloneRows = @()
foreach ($day in (As-Array $clones.clones)) {
    $cloneRows += [pscustomobject]@{
        date           = ([datetime]$day.timestamp).ToUniversalTime().ToString("yyyy-MM-dd")
        clones         = (Get-JsonInt $day "count")
        unique_cloners = (Get-JsonInt $day "uniques")
        collected_at   = $collectedAt
    }
}
Upsert-CsvRows -FilePath (Join-Path $OutDir "clones_daily.csv") `
    -Header @("date", "clones", "unique_cloners", "collected_at") `
    -Rows $cloneRows `
    -KeyColumns @("date")

# --- snapshot rollup (14d totals at collection time) ---
$snapshotDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
$snapshotRow = @(
    [pscustomobject]@{
        date              = $snapshotDate
        views_14d         = (Get-JsonInt $views "count")
        unique_views_14d  = (Get-JsonInt $views "uniques")
        clones_14d        = (Get-JsonInt $clones "count")
        unique_clones_14d = (Get-JsonInt $clones "uniques")
        collected_at      = $collectedAt
    }
)
Upsert-CsvRows -FilePath (Join-Path $OutDir "snapshots_14d.csv") `
    -Header @("date", "views_14d", "unique_views_14d", "clones_14d", "unique_clones_14d", "collected_at") `
    -Rows $snapshotRow `
    -KeyColumns @("date")

# --- referrers / popular paths (snapshot; GitHub only returns top lists) ---
$referrerRows = @()
foreach ($r in $referrers) {
    $referrerRows += [pscustomobject]@{
        date            = $snapshotDate
        referrer        = [string]$r.referrer
        views           = (Get-JsonInt $r "count")
        unique_visitors = (Get-JsonInt $r "uniques")
        collected_at    = $collectedAt
    }
}
if ($referrerRows.Count -gt 0) {
    Upsert-CsvRows -FilePath (Join-Path $OutDir "referrers.csv") `
        -Header @("date", "referrer", "views", "unique_visitors", "collected_at") `
        -Rows $referrerRows `
        -KeyColumns @("date", "referrer")
}

$pathRows = @()
foreach ($p in $paths) {
    $pathRows += [pscustomobject]@{
        date            = $snapshotDate
        path            = [string]$p.path
        title           = [string]$p.title
        views           = (Get-JsonInt $p "count")
        unique_visitors = (Get-JsonInt $p "uniques")
        collected_at    = $collectedAt
    }
}
if ($pathRows.Count -gt 0) {
    Upsert-CsvRows -FilePath (Join-Path $OutDir "popular_paths.csv") `
        -Header @("date", "path", "title", "views", "unique_visitors", "collected_at") `
        -Rows $pathRows `
        -KeyColumns @("date", "path")
}

# --- release asset downloads (all-time cumulative; no 14-day limit) ---
$releases = As-Array (Invoke-GhJson "$repoPath/releases?per_page=100")
$downloadRows = @()
foreach ($rel in $releases) {
    foreach ($asset in (As-Array $rel.assets)) {
        $downloadRows += [pscustomobject]@{
            date           = $snapshotDate
            tag            = [string]$rel.tag_name
            asset          = [string]$asset.name
            download_count = (Get-JsonInt $asset "download_count")
            collected_at   = $collectedAt
        }
    }
}
if ($downloadRows.Count -gt 0) {
    Upsert-CsvRows -FilePath (Join-Path $OutDir "release_downloads.csv") `
        -Header @("date", "tag", "asset", "download_count", "collected_at") `
        -Rows $downloadRows `
        -KeyColumns @("date", "tag", "asset")
}

Write-Host "Done. CSVs in: $OutDir"
Write-Host ("  views_14d={0} unique={1} | clones_14d={2} unique={3}" -f `
    (Get-JsonInt $views "count"), (Get-JsonInt $views "uniques"),
    (Get-JsonInt $clones "count"), (Get-JsonInt $clones "uniques"))
Get-ChildItem $OutDir -Filter *.csv | ForEach-Object {
    Write-Host ("  - {0}" -f $_.Name)
}
