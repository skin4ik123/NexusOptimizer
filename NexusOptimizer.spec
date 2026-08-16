# -*- mode: python ; coding: utf-8 -*-
"""Build spec for Nexus Optimizer: a portable onefile exe and the onedir tree
that the installer ships."""
import os
from PyInstaller.utils.hooks import collect_all

# No "twain" here: TWAIN is called through a first-party ctypes binding, so no
# separate package is needed (see requirements.txt).
hiddenimports = ["fitz", "pymupdf", "darkdetect",
                 "PIL.Image", "PIL.ImageOps", "PIL.ImageStat"]
datas, binaries = [], []
for pkg in ("customtkinter", "tkinterdnd2", "imageio_ffmpeg", "comtypes",
            "pikepdf", "pillow_heif"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# The TWAIN Data Source Manager, when it sits next to the project. A 64-bit
# twaindsm.dll is not installed by every scanner driver, and without one TWAIN
# sees no devices at all. The file is optional: the build works without it.
if os.path.exists("twaindsm.dll"):
    binaries += [("twaindsm.dll", ".")]

a = Analysis(
    ["NexusPDFOptimizer.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["numpy", "pandas", "scipy", "matplotlib",
              "PyQt5", "PySide6", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# The PyInstaller splash screen is deliberately unused: there must be exactly
# one loading window, and the application draws its own (_boot_splash_thread).
_icon = "nexus.ico" if os.path.exists("nexus.ico") else None

# 1) Portable single file: dist/NexusOptimizer.exe
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NexusOptimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=_icon,
)

# 2) onedir build for the installer: dist/NexusOptimizer/. Nothing is unpacked
# at startup, so the program opens in under a second. The modules (ffmpeg,
# tkdnd, comtypes, HEIF) live in _internal next to the exe.
exe_dir = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NexusOptimizer",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=_icon,
)
coll = COLLECT(
    exe_dir,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NexusOptimizer",
)
