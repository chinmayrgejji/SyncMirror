# Installs SyncMirror for the current user so it's launchable from the Start
# menu / search and a desktop shortcut -- no need to open the exe manually.
# Run:  powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"

$AppName   = "SyncMirror"
$SourceDir = Join-Path $PSScriptRoot "dist\SyncMirror"
$ExeName   = "SyncMirror.exe"

if (-not (Test-Path (Join-Path $SourceDir $ExeName))) {
    Write-Error "Build output not found at '$SourceDir'. Run the PyInstaller build first."
}

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$ExePath    = Join-Path $InstallDir $ExeName

Write-Host "Installing $AppName to $InstallDir ..."

# Stop any running instance so files aren't locked during copy.
Get-Process -Name $AppName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item (Join-Path $SourceDir "*") $InstallDir -Recurse -Force

# Create Start-menu and Desktop shortcuts pointing at the installed exe.
$IconPath  = Join-Path $InstallDir "SyncMirror.ico"
if (-not (Test-Path $IconPath)) { $IconPath = $ExePath }

$shell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$targets = @(
    (Join-Path $startMenu "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")
)
foreach ($lnk in $targets) {
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath       = $ExePath
    $sc.WorkingDirectory = $InstallDir
    $sc.IconLocation     = $IconPath
    $sc.Description       = "Synchronized audio mirroring across Bluetooth devices"
    $sc.Save()
}

Write-Host "Done. Search '$AppName' in the Start menu or use the desktop shortcut."
Write-Host "Installed exe: $ExePath"
