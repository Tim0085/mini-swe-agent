$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$archiveRoot = [IO.Path]::GetFullPath((Join-Path $taskRoot '.local\archive\install'))
if (-not $archiveRoot.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive is outside project' }
New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
$mapping = @()
foreach ($fileName in @('install.log', 'socks-install.log')) {
    $source = Join-Path $taskRoot $fileName
    $target = Join-Path $archiveRoot $fileName
    if (Test-Path -LiteralPath $source) {
        if (Test-Path -LiteralPath $target) { throw "Archive already exists: $fileName" }
        $stream = [IO.File]::Open($source, 'Open', 'Read', 'None')
        $stream.Dispose()
        $hashBefore = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        Move-Item -LiteralPath $source -Destination $target
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $hashBefore) { throw 'Archive hash mismatch' }
        $mapping += @{ source = $source; destination = $target; sha256 = $hashBefore; bytes = (Get-Item -LiteralPath $target).Length }
    }
}
if ($mapping.Count) {
    [IO.File]::WriteAllText((Join-Path $archiveRoot 'manifest.json'), ($mapping | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
}
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDir 'Mini 工作台.lnk'
$ws = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $existing = $ws.CreateShortcut($shortcutPath)
    if ($existing.Arguments -notlike '*local_setup\webui\launch.py*') { throw 'An unrelated desktop shortcut already exists.' }
}
$shortcut = $ws.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
$shortcut.Arguments = '"' + (Join-Path $taskRoot 'local_setup\webui\launch.py') + '"'
$shortcut.WorkingDirectory = $taskRoot
$shortcut.Description = 'Mini 本地编程与评测工作台'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output ('Archived logs: ' + $mapping.Count)
Write-Output ('Desktop shortcut: ' + $shortcutPath)
