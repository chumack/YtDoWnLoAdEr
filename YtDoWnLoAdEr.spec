# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для YtDoWnLoAdEr.

Использование:
    pyinstaller YtDoWnLoAdEr.spec              # onedir (по умолчанию)
    pyinstaller YtDoWnLoAdEr.spec -- --onefile # одиночный .exe

onedir запускается быстрее и дружит с антивирусами лучше, чем onefile.
"""
import sys

from PyInstaller.utils.hooks import collect_all

onefile = "--onefile" in sys.argv
if onefile:
    sys.argv.remove("--onefile")

# Собираем данные ЗАРАНЕЕ и передаём в Analysis (append в a.datas ломает COLLECT)
extra_datas = []
extra_binaries = []
extra_hidden = [
    "yt_dlp",
    "yt_dlp.extractor",
    "yt_dlp.postprocessor",
]
for pkg in ("yt_dlp", "customtkinter"):
    try:
        d, b, h = collect_all(pkg)
        extra_datas += d
        extra_binaries += b
        extra_hidden += h
    except Exception:
        pass

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if onefile:
    EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="YtDoWnLoAdEr",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="YtDoWnLoAdEr",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="YtDoWnLoAdEr",
    )
