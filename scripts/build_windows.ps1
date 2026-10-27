# Build the Windows desktop app and the Inno Setup installer.
# Run this on Windows from any directory:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#
# A Windows installer cannot be produced on Linux. PyInstaller does not
# cross-compile, and Inno Setup runs on Windows. The GitHub Actions workflow
# .github/workflows/windows-installer.yml runs this script on windows-latest.

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") {
    Write-Error "Build the installer on Windows. On Linux, download it from the GitHub Release, or from the artifact on a pull request run of the Windows installer workflow."
}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    Write-Error "Python 3.11+ is required and must be on PATH."
}

python -m pip install --upgrade pip
python -m pip install -r "$Root\requirements.txt" -r "$Root\requirements-build.txt"

$work = Join-Path $Root "packaging\pyi-build"
if (Test-Path $work) {
    Remove-Item -Recurse -Force $work
}
python -m PyInstaller --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath $work "$Root\packaging\rummagarr.spec"

function Find-InnoCompiler {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $installer = Join-Path $env:TEMP "innosetup-6.exe"
    Write-Host "Downloading Inno Setup 6..."
    Invoke-WebRequest -Uri "https://jrsoftware.org/download.php/is.exe" -OutFile $installer
    Start-Process -FilePath $installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-" -Wait
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    throw "Inno Setup 6 installed but ISCC.exe was not found."
}

$iscc = Find-InnoCompiler
$version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
& $iscc "/DMyAppVersion=$version" (Join-Path $Root "packaging\Rummagarr.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

$setup = Join-Path $Root "dist\Rummagarr-Setup-$version.exe"
if (-not (Test-Path $setup)) {
    throw "Installer was not written: $setup"
}
Write-Host "Installer: $setup"
