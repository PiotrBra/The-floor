# -*- mode: python ; coding: utf-8 -*-
# Build:  poetry run pyinstaller TheFloor.spec
#   macOS    -> dist/TheFloor.app
#   Windows  -> dist/TheFloor/TheFloor.exe (+ _internal/)
import sys
from pathlib import Path

block_cipher = None

# Walk the images tree but skip archives — the running app reads loose files.
_IMAGE_EXTS = {'.jpg', '.jpeg', '.jfif', '.png', '.bmp', '.gif', '.webp', '.avif'}
_image_datas = [
    (str(p), str(p.parent).replace('\\', '/'))
    for p in Path('images').rglob('*')
    if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[*_image_datas, ('excel.csv', '.')],
    hiddenimports=[
        'pillow_avif',
        'pillow_avif.AvifImagePlugin',
    ],
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TheFloor',
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TheFloor',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='TheFloor.app',
        icon=None,
        bundle_identifier='com.thefloor.app',
        info_plist={
            'CFBundleName': 'The Floor',
            'CFBundleDisplayName': 'The Floor',
            'CFBundleVersion': '0.1.0',
            'CFBundleShortVersionString': '0.1.0',
            'NSHighResolutionCapable': True,
        },
    )
