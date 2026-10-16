param(
    [Parameter(Mandatory=$true)][string]$PackageZip,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][int]$ExistingProcessId,
    [Parameter(Mandatory=$true)][string]$LaunchPath,
    [Parameter(Mandatory=$true)][string]$ErrorLog
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallDir)
$installParent = Split-Path -Parent $installPath
$installName = Split-Path -Leaf $installPath
$operationId = [guid]::NewGuid().ToString('N')
$extractDir = Join-Path ([IO.Path]::GetTempPath()) ('HFDownloader-update-' + $operationId)
$stagedInstall = Join-Path $installParent ('.' + $installName + '.update-' + $operationId)
$backupInstall = Join-Path $installParent ('.' + $installName + '.backup-' + $operationId)
$movedOldInstall = $false
$installedNew = $false
$committed = $false

try {
    Wait-Process -Id $ExistingProcessId -Timeout 60 -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $extractDir,$installParent -Force | Out-Null
    Expand-Archive -LiteralPath $PackageZip -DestinationPath $extractDir -Force

    $program = Get-ChildItem -LiteralPath $extractDir -Filter 'HFDownloader.exe' -File -Recurse | Select-Object -First 1
    if (-not $program) { throw '更新包中没有 HFDownloader.exe。' }
    $packageRoot = $program.Directory.FullName
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot '_internal\vendor\aria2c.exe') -PathType Leaf)) {
        throw '更新包缺少下载引擎，原程序未更改。'
    }

    New-Item -ItemType Directory -Path $stagedInstall -Force | Out-Null
    Get-ChildItem -LiteralPath $packageRoot -Force | Copy-Item -Destination $stagedInstall -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $stagedInstall 'HFDownloader.exe') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $stagedInstall '_internal\vendor\aria2c.exe') -PathType Leaf)) {
        throw '更新包复制到暂存目录后校验失败，原程序未更改。'
    }

    if (Test-Path -LiteralPath $installPath) {
        Move-Item -LiteralPath $installPath -Destination $backupInstall
        $movedOldInstall = $true
    }
    Move-Item -LiteralPath $stagedInstall -Destination $installPath
    $installedNew = $true

    if (-not (Test-Path -LiteralPath (Join-Path $installPath 'HFDownloader.exe') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $installPath '_internal\vendor\aria2c.exe') -PathType Leaf)) {
        throw '安装目录校验失败，正在恢复旧版本。'
    }

    $newProcess = Start-Process -FilePath $LaunchPath -PassThru
    Start-Sleep -Seconds 3
    $newProcess.Refresh()
    if ($newProcess.HasExited) { throw '新版本启动失败，正在恢复旧版本。' }
    $committed = $true
    if (Test-Path -LiteralPath $ErrorLog) { Remove-Item -LiteralPath $ErrorLog -Force -ErrorAction SilentlyContinue }
    if ($movedOldInstall -and (Test-Path -LiteralPath $backupInstall)) {
        Remove-Item -LiteralPath $backupInstall -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $PackageZip -Force -ErrorAction SilentlyContinue
}
catch {
    if (-not $committed -and $movedOldInstall -and (Test-Path -LiteralPath $backupInstall)) {
        if ($installedNew -and (Test-Path -LiteralPath $installPath)) {
            Remove-Item -LiteralPath $installPath -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-Path -LiteralPath $installPath)) {
            Move-Item -LiteralPath $backupInstall -Destination $installPath -ErrorAction SilentlyContinue
        }
    }
    $directory = Split-Path -Parent $ErrorLog
    if ($directory) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $_.Exception.Message | Set-Content -LiteralPath $ErrorLog -Encoding UTF8
}
finally {
    if (Test-Path -LiteralPath $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stagedInstall) {
        Remove-Item -LiteralPath $stagedInstall -Recurse -Force -ErrorAction SilentlyContinue
    }
}
