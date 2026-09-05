; Inno Setup script -- builds a single shareable SyncMirror-Setup.exe.
; Compile with: "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" SyncMirror.iss
; (or run build_installer.ps1 which locates ISCC automatically)

#define AppName "SyncMirror"
#define AppVersion "1.0.0"
#define AppPublisher "Chinmay Gejji"
#define AppExe "SyncMirror.exe"

[Setup]
AppId={{9BF38D6E-EF9D-4716-B998-2D97491248EC}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install: no admin prompt, so it's easy for others to run.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=SyncMirror.ico
OutputDir=Output
OutputBaseFilename=SyncMirror-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close the app automatically if it's running during install/uninstall.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Bundle the entire PyInstaller onedir output.
Source: "dist\SyncMirror\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Also place the icon at the app root so shortcuts can reference a stable path.
Source: "SyncMirror.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\SyncMirror.ico"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\SyncMirror.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
