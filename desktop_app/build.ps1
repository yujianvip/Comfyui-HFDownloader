param([switch]$StageOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$stage = Join-Path $projectRoot 'build\pyinstaller-package-stage'
$dist = Join-Path $stage 'dist'
$work = Join-Path $stage 'work'
$releaseRoot = Join-Path $projectRoot 'release'
$package = Join-Path $releaseRoot 'HFDownloader'
$backup = Join-Path $releaseRoot '.HFDownloader-backup'

if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv\Scripts\python.exe.' }
if (-not $StageOnly -and (Get-Process HFDownloader -ErrorAction SilentlyContinue)) { throw 'Close HFDownloader before building.' }
if (-not $StageOnly -and (Get-Process aria2c -ErrorAction SilentlyContinue)) { throw 'Wait for aria2c downloads to finish before replacing the release package.' }

if (Test-Path -LiteralPath $backup) {
    if (Test-Path -LiteralPath $package) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    else {
        Move-Item -LiteralPath $backup -Destination $package
    }
}
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage,$releaseRoot -Force | Out-Null

$sourceSnapshot = Join-Path $stage 'source'
$sourceFiles = @(
    'app.py','core.py','test_download.py','LICENSE.txt','THIRD_PARTY.txt',
    'vendor\aria2c.exe','vendor\aria2-COPYING.txt','vendor\LICENSE.OpenSSL',
    'vendor\aria2-AUTHORS.txt','vendor\Python-LICENSE.txt','vendor\Tcl-license.terms',
    'vendor\aria2-release-1.37.0.tar.gz'
)
$sourceHashes = @{}
foreach ($relative in $sourceFiles) {
    $source = Join-Path $PSScriptRoot $relative
    $snapshot = Join-Path $sourceSnapshot $relative
    New-Item -ItemType Directory -Path (Split-Path $snapshot) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $snapshot -Force
    $sourceHashes[$relative] = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
}

$appText = Get-Content -LiteralPath (Join-Path $sourceSnapshot 'app.py') -Raw -Encoding UTF8
$versionMatch = [regex]::Match($appText, "(?m)^APP_VERSION\s*=\s*'(?<version>\d+\.\d+\.\d+)'\s*$")
if (-not $versionMatch.Success) { throw 'APP_VERSION is missing or invalid in app.py.' }
$version = $versionMatch.Groups['version'].Value
$versionParts = @($version.Split('.') | ForEach-Object { [int]$_ })
$versionTuple = '{0}, {1}, {2}, 0' -f $versionParts[0],$versionParts[1],$versionParts[2]
$versionText = $version + '.0'
$versionFile = Join-Path $stage 'version_info.txt'
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionTuple), prodvers=($versionTuple), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'yujian5455'),
      StringStruct('FileDescription', 'Hugging Face Model Downloader'),
      StringStruct('FileVersion', '$versionText'),
      StringStruct('InternalName', 'HFDownloader'),
      StringStruct('OriginalFilename', 'HFDownloader.exe'),
      StringStruct('ProductName', 'HF Downloader'),
      StringStruct('ProductVersion', '$versionText')
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -LiteralPath $versionFile -Encoding UTF8

$previousUserBase = [Environment]::GetEnvironmentVariable('PYTHONUSERBASE','Process')
try {
    # PyInstaller 需要查询用户级 site-packages。把查询位置限制在本次临时目录，
    # 避免读取系统 Python、ComfyUI 或当前 Windows 用户的全局包。
    [Environment]::SetEnvironmentVariable('PYTHONUSERBASE',(Join-Path $stage 'isolated-userbase'),'Process')
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir --name HFDownloader `
        --distpath $dist --workpath $work --specpath $stage --version-file $versionFile `
        --add-binary "$sourceSnapshot\vendor\aria2c.exe;vendor" (Join-Path $sourceSnapshot 'app.py')
    $pyInstallerExitCode = $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable('PYTHONUSERBASE',$previousUserBase,'Process')
}
if ($pyInstallerExitCode -ne 0) { throw 'PyInstaller build failed.' }

$built = Join-Path $dist 'HFDownloader'
if (-not (Test-Path -LiteralPath (Join-Path $built 'HFDownloader.exe'))) { throw 'Built HFDownloader.exe is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $built '_internal\vendor\aria2c.exe'))) { throw 'Bundled aria2c.exe is missing.' }

$packageParent = Join-Path $stage 'package'
New-Item -ItemType Directory -Path $packageParent -Force | Out-Null
Copy-Item -LiteralPath $built -Destination $packageParent -Recurse -Force
$assembled = Join-Path $packageParent 'HFDownloader'
$licenses = Join-Path $assembled 'licenses'
$sources = Join-Path $assembled 'sources'
$appSources = Join-Path $sources 'hf-downloader'
New-Item -ItemType Directory -Path $licenses,$sources,$appSources -Force | Out-Null
Copy-Item -LiteralPath @((Join-Path $sourceSnapshot 'THIRD_PARTY.txt'),(Join-Path $sourceSnapshot 'LICENSE.txt')) -Destination $assembled -Force
Copy-Item -LiteralPath @((Join-Path $sourceSnapshot 'vendor\aria2-COPYING.txt'),(Join-Path $sourceSnapshot 'vendor\LICENSE.OpenSSL'),(Join-Path $sourceSnapshot 'vendor\aria2-AUTHORS.txt'),(Join-Path $sourceSnapshot 'vendor\Python-LICENSE.txt'),(Join-Path $sourceSnapshot 'vendor\Tcl-license.terms')) -Destination $licenses -Force
Copy-Item -LiteralPath (Join-Path $sourceSnapshot 'vendor\aria2-release-1.37.0.tar.gz') -Destination $sources -Force
Copy-Item -LiteralPath @((Join-Path $sourceSnapshot 'app.py'),(Join-Path $sourceSnapshot 'core.py'),(Join-Path $sourceSnapshot 'test_download.py'),(Join-Path $sourceSnapshot 'LICENSE.txt'),(Join-Path $sourceSnapshot 'THIRD_PARTY.txt')) -Destination $appSources -Force

if (Test-Path -LiteralPath (Join-Path $assembled 'data')) { throw 'Release package must not contain runtime data.' }
if (Test-Path -LiteralPath (Join-Path $assembled 'downloads')) { throw 'Release package must not contain user downloads.' }
$changedSources = @($sourceFiles | Where-Object {
    (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot $_)).Hash -ne $sourceHashes[$_]
})
if ($changedSources.Count) {
    throw ('Source files changed during build; release package was not replaced: ' + ($changedSources -join ', '))
}
if ($StageOnly) {
    Write-Host ('Staged: ' + $assembled)
    return
}

$hadPackage = Test-Path -LiteralPath $package
$movedOldPackage = $false
$installedNewPackage = $false
try {
    if ($hadPackage) {
        Move-Item -LiteralPath $package -Destination $backup
        $movedOldPackage = $true
    }
    Move-Item -LiteralPath $assembled -Destination $package
    $installedNewPackage = $true
    if (-not (Test-Path -LiteralPath (Join-Path $package 'HFDownloader.exe'))) { throw 'Installed HFDownloader.exe is missing.' }
    if (-not (Test-Path -LiteralPath (Join-Path $package '_internal\vendor\aria2c.exe'))) { throw 'Installed aria2c.exe is missing.' }
    if (Test-Path -LiteralPath (Join-Path $package 'data')) { throw 'Installed package contains runtime data.' }
    if (Test-Path -LiteralPath (Join-Path $package 'downloads')) { throw 'Installed package contains user downloads.' }
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
}
catch {
    $failure = $_
    if ($movedOldPackage -and (Test-Path -LiteralPath $backup)) {
        if ($installedNewPackage -and (Test-Path -LiteralPath $package)) {
            Remove-Item -LiteralPath $package -Recurse -Force
        }
        if (-not (Test-Path -LiteralPath $package)) {
            Move-Item -LiteralPath $backup -Destination $package
        }
    }
    throw $failure
}

Remove-Item -LiteralPath $stage -Recurse -Force
$specFile = Join-Path $PSScriptRoot 'HFDownloader.spec'
if (Test-Path -LiteralPath $specFile) { Remove-Item -LiteralPath $specFile -Force }
$buildRoot = Split-Path $stage
if ((Test-Path -LiteralPath $buildRoot) -and @(Get-ChildItem -LiteralPath $buildRoot -Force).Count -eq 0) {
    Remove-Item -LiteralPath $buildRoot -Force
}
Write-Host ('Updated: ' + $package)
Write-Host ('Version: ' + $version)
Write-Host 'No ZIP was created. Runtime data is stored outside the program folder.'
