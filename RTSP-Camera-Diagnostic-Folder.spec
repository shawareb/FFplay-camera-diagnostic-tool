# -*- mode: python ; coding: utf-8 -*-
# One-folder EXE build specification for RTSP Camera Diagnostic Tool.
# Produces a folder distribution with lower AV false-positive risk.
# Build with:  python -m PyInstaller RTSP-Camera-Diagnostic-Folder.spec

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets\\camera_icon.png', 'assets'),
        ('assets\\camera_icon.ico', 'assets'),
    ],
    hiddenimports=[
        'fpdf',
        'fpdf.enums',
        'matplotlib',
        'matplotlib.pyplot',
        'matplotlib.backends.backend_agg',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['test', 'pytest', 'tkinter.test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RTSP-Camera-Diagnostic-Folder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\camera_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='RTSP-Camera-Diagnostic-Folder',
)
