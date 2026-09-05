# Building the SyncMirror EXE

Reference notes for producing `SyncMirror.exe` from source.

## Prerequisites

- Windows (the app requires WASAPI via `pyaudiowpatch`).
- Python 3.14 (the environment used to build).
- Project dependencies installed:

```powershell
pip install -r requirements.txt
```

- PyInstaller installed:

```powershell
pip install pyinstaller
```

## Build command

Run from the project root (`SyncMirror\`):

```powershell
python -m PyInstaller --noconfirm --clean SyncMirror.spec
```

Output: `dist\SyncMirror\` (a **onedir** bundle) containing `SyncMirror.exe`
plus its dependencies. Run `SyncMirror.exe` from that folder.

To produce a shareable installer instead (recommended), use
`build_installer.ps1` — see **Building the installer** below.

## What the build includes

Defined in `SyncMirror.spec`:

- **Entry point:** `src/app.py`
- **Icon:** `SyncMirror.ico` — both embedded in the exe (Explorer/taskbar) and
  bundled as data so `resource_path()` can set the titlebar icon at runtime.
- **`console=False`** — no console window (GUI app).
- **`collect_all`** for `customtkinter`, `pyaudiowpatch`, `winrt`, `pystray`,
  `flask`, `werkzeug`, `jinja2`, and `qrcode` so their data files, DLLs, and
  submodules are pulled in.
- **Hidden imports** for the `winrt.windows.*` submodules used at runtime
  (media control, storage streams, device enumeration, media devices,
  foundation) and the local `webremote` module.

## Icon wiring in the app (`src/app.py`)

- `resource_path(relative)` resolves bundled files both from source and from a
  PyInstaller bundle (via `sys._MEIPASS`).
- `ICON_PATH = resource_path("SyncMirror.ico")`.
- `root.iconbitmap(ICON_PATH)` is set in `App.__init__` and in `main()`.
- `SetCurrentProcessExplicitAppUserModelID("SyncMirror.App")` in `main()` makes
  the Windows taskbar use the app icon instead of the generic Python icon.

## Building the installer

`build_installer.ps1` rebuilds the onedir bundle and then compiles the Inno
Setup script (`SyncMirror.iss`) into a single shareable installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_installer.ps1
```

Output: `Output\SyncMirror-Setup.exe`. It's a **per-user** installer (no admin
prompt) that installs to `%LOCALAPPDATA%\Programs\SyncMirror` and creates Start
menu + optional desktop shortcuts and an Add/Remove Programs entry.

Requires Inno Setup 6 (`winget install JRSoftware.InnoSetup`); the script locates
`ISCC.exe` automatically.

Silent (re)install for testing:

```powershell
.\Output\SyncMirror-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon
```

Alternatively, `install.ps1` installs straight from `dist\SyncMirror` (copies
the folder + makes shortcuts) without producing an installer exe.

## Two exe files, explained

- `Output\SyncMirror-Setup.exe` — the **installer** (run once / share with
  others).
- `%LOCALAPPDATA%\Programs\SyncMirror\SyncMirror.exe` — the **installed app**
  the Start-menu/desktop shortcuts launch.

## Troubleshooting

- **Missing `winrt` submodule at runtime:** add the failing module to
  `hiddenimports` in `SyncMirror.spec`, then rebuild.
- **See the real traceback:** temporarily set `console=True` in the spec (or
  build with `--console`) and launch from a terminal.
- **Icon not updating:** Windows caches icons — the AppUserModelID plus a fresh
  copy of the exe normally forces a refresh.

## Build artifacts

- `dist\SyncMirror\` — the onedir app bundle (contains `SyncMirror.exe`).
- `Output\SyncMirror-Setup.exe` — the compiled installer.
- `build\` — intermediate files (safe to delete; regenerated each build).
- `SyncMirror.spec` / `SyncMirror.iss` — the build + installer recipes (keep in
  version control).
