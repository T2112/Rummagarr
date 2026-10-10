# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows desktop app.

Run from the repo root, normally via scripts/build_windows.ps1.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).resolve().parent
hidden: list[str] = []
for package in ("aioslsk", "httpx", "httpcore", "anyio", "pydantic", "async_upnp_client", "mutagen"):
    hidden += collect_submodules(package)

datas = [
    (str(root / "config.example.toml"), "."),
    (str(root / "VERSION"), "."),
    (str(root / "LICENSE.txt"), "."),
]
datas += collect_data_files("certifi")

a = Analysis(
    [str(root / "desktop_app.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LidarrSoulseek",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="LidarrSoulseek",
)
