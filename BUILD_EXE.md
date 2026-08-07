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

Output: `dist\SyncMirror.exe` (one-file, windowed, ~32 MB). Copy that single
file anywhere — it runs standalone.

## What the build includes

Defined in `SyncMirror.spec`:

- **Entry point:** `src/app.py`
- **Icon:** `SyncMirror.ico` — both embedded in the exe (Explorer/taskbar) and
  bundled as data so `resource_path()` can set the titlebar icon at runtime.
- **`console=False`** — no console window (GUI app).
- **`collect_all`** for `customtkinter`, `pyaudiowpatch`, and `winrt` so their
  data files, DLLs, and submodules are pulled in.
- **Hidden imports** for the `winrt.windows.*` submodules used at runtime
  (media control, storage streams, device enumeration, media devices,
  foundation).

## Icon wiring in the app (`src/app.py`)

- `resource_path(relative)` resolves bundled files both from source and from a
  PyInstaller bundle (via `sys._MEIPASS`).
- `ICON_PATH = resource_path("SyncMirror.ico")`.
- `root.iconbitmap(ICON_PATH)` is set in `App.__init__` and in `main()`.
- `SetCurrentProcessExplicitAppUserModelID("SyncMirror.App")` in `main()` makes
  the Windows taskbar use the app icon instead of the generic Python icon.

## Troubleshooting

- **Missing `winrt` submodule at runtime:** add the failing module to
  `hiddenimports` in `SyncMirror.spec`, then rebuild.
- **See the real traceback:** temporarily set `console=True` in the spec (or
  build with `--console`) and launch from a terminal.
- **Icon not updating:** Windows caches icons — the AppUserModelID plus a fresh
  copy of the exe normally forces a refresh.

## Build artifacts

- `dist\` — the final `SyncMirror.exe`.
- `build\` — intermediate files (safe to delete; regenerated each build).
- `SyncMirror.spec` — the build recipe (keep in version control).
