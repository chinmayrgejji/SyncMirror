# Builds the shareable installer: rebuilds the exe (onedir) then compiles the
# Inno Setup script into Output\SyncMirror-Setup.exe.
# Run:  powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

$ErrorActionPreference = "Stop"

# 1. Rebuild the PyInstaller onedir bundle.
python -m PyInstaller --noconfirm --clean SyncMirror.spec

# 2. Locate the Inno Setup compiler.
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Error "ISCC.exe not found. Install Inno Setup: winget install JRSoftware.InnoSetup"
}

# 3. Compile the installer.
& $iscc "SyncMirror.iss"

Write-Host "Installer built: $(Join-Path $PSScriptRoot 'Output\SyncMirror-Setup.exe')"
