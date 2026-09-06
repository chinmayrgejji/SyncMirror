# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [("SyncMirror.ico", "."), ("assets/fonts", "assets/fonts")]
binaries = []
hiddenimports = [
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
    "winrt.windows.devices.enumeration",
    "winrt.windows.media.devices",
    "winrt.windows.foundation",
    "webremote",
]

for pkg in ("customtkinter", "pyaudiowpatch", "winrt", "pystray", "flask", "werkzeug", "jinja2", "qrcode", "scipy"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["src/app.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SyncMirror",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="SyncMirror.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SyncMirror",
)
