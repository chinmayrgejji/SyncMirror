# Removes SyncMirror installed by install.ps1.
# Run:  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

$ErrorActionPreference = "SilentlyContinue"

$AppName    = "SyncMirror"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$startMenu  = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
$desktop    = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"

Get-Process -Name $AppName -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item $InstallDir -Recurse -Force
Remove-Item $startMenu -Force
Remove-Item $desktop -Force

Write-Host "$AppName uninstalled."
