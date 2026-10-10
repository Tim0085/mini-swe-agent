param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$plan = Get-Content -LiteralPath (Join-Path $installRoot '.local\cleanup-eval-audit\cleanup-plan.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$evalTarget = [IO.Path]::GetFullPath((Join-Path $installRoot '.local\eval-venv'))
$cacheRoot = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'pip\Cache\http-v2'))

if ([IO.Path]::GetFullPath($plan.environment) -ne $evalTarget) {
    throw 'Cleanup manifest does not match this installation.'
}
$active = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(python|pip).*\.exe$' -and
    ($_.ExecutablePath -like "$evalTarget\*" -or $_.CommandLine -match '\bpip\b.*\binstall\b')
})
if ($active.Count) { throw 'A Python package installation is running. Finish or cancel it before cleanup.' }

$evalBytes = 0
if (Test-Path -LiteralPath $evalTarget) {
    $evalItem = Get-Item -LiteralPath $evalTarget -Force
    if ($evalItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a linked environment.' }
    $entries = @(Get-ChildItem -LiteralPath $evalTarget -Recurse -Force)
    if (@($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
        throw 'Refusing an environment containing links.'
    }
    $evalBytes = ($entries | Where-Object { -not $_.PSIsContainer } | Measure-Object Length -Sum).Sum
    if ($evalBytes -ne $plan.environment_bytes) { throw 'The environment changed after inspection; cleanup stopped.' }
}

$cacheFiles = @()
foreach ($record in $plan.cache_files) {
    $target = [IO.Path]::GetFullPath($record.path)
    if (-not $target.StartsWith($cacheRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A cache path is outside the approved directory.'
    }
    if (-not (Test-Path -LiteralPath $target)) { continue }
    $item = Get-Item -LiteralPath $target -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing a directory or link in the cache file list.'
    }
    if ($item.Length -ne $record.bytes -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $record.sha256) {
        throw 'A cache file changed after inspection; cleanup stopped.'
    }
    $cacheFiles += $record
}

$protectedNames = @('.venv\pyvenv.cfg', '.local\.env', '.local\api-key.dpapi',
    '.local\django__django-10097.traj.json',
    '.local\django__django-10097-20260914-224547.predictions.jsonl')
$protectedHashes = @{}
foreach ($name in $protectedNames) {
    $path = Join-Path $installRoot $name
    $protectedHashes[$path] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}
$totalBytes = $evalBytes + ($cacheFiles | Measure-Object bytes -Sum).Sum
Write-Host ('Verified cleanup: {0:N2} MiB; {1} cache files plus the unused eval-venv.' -f ($totalBytes / 1MB), $cacheFiles.Count)
if ($CheckOnly) { Write-Host 'CHECK ONLY: no files deleted.'; return }

if (Test-Path -LiteralPath $evalTarget) { Remove-Item -LiteralPath $evalTarget -Recurse -Force }
foreach ($record in $cacheFiles) { Remove-Item -LiteralPath $record.path -Force }
if (Test-Path -LiteralPath $evalTarget) { throw 'Evaluation environment still exists.' }
foreach ($record in $cacheFiles) {
    if (Test-Path -LiteralPath $record.path) { throw 'A selected cache file still exists.' }
}
foreach ($path in $protectedHashes.Keys) {
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $protectedHashes[$path]) {
        throw 'A protected file changed.'
    }
}
Write-Host ('Cleanup complete: removed {0:N2} MiB. Original mini environment, config, key and results preserved.' -f ($totalBytes / 1MB))
