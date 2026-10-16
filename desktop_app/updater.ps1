param(
    [Parameter(Mandatory=$true)][string]$PackageZip,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][int]$ExistingProcessId,
    [Parameter(Mandatory=$true)][string]$LaunchPath,
    [Parameter(Mandatory=$true)][string]$ErrorLog
)

$ErrorActionPreference = 'Stop'
$extractDir = Join-Path ([IO.Path]::GetTempPath()) ('HFDownloader-update-' + [guid]::NewGuid().ToString('N'))
try {
    Wait-Process -Id $ExistingProcessId -Timeout 60 -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
    Expand-Archive -LiteralPath $PackageZip -DestinationPath $extractDir -Force
    $program = Get-ChildItem -LiteralPath $extractDir -Filter 'HFDownloader.exe' -File -Recurse | Select-Object -First 1
    if (-not $program) { throw '更新包中没有 HFDownloader.exe。' }
    $packageRoot = $program.Directory.FullName
    $updated = $false
    for ($attempt=1; $attempt -le 20; $attempt++) {
        try {
            Copy-Item -Path (Join-Path $packageRoot '*') -Destination $InstallDir -Recurse -Force
            $updated = $true
            break
        }
        catch {
            if ($attempt -eq 20) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $updated) { throw '无法替换程序文件。' }
    if (Test-Path -LiteralPath $ErrorLog) { Remove-Item -LiteralPath $ErrorLog -Force }
    Start-Process -FilePath $LaunchPath
    Remove-Item -LiteralPath $PackageZip -Force -ErrorAction SilentlyContinue
}
catch {
    $directory = Split-Path -Parent $ErrorLog
    if ($directory) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $_.Exception.Message | Set-Content -LiteralPath $ErrorLog -Encoding UTF8
}
finally {
    if (Test-Path -LiteralPath $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
}
