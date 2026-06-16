# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — StratoPi HSO Ground Station (Windows)
# Build: pyinstaller StratoPiGroundStation.spec

import sys
from pathlib import Path

block_cipher = None
root = Path(SPECPATH)

a = Analysis(
    ['gs_app.py'],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'templates'), 'templates')],
    hiddenimports=[
        'bundle_paths',
        'alerts',
        'e22_regs',
        'lora_crypto',
        'lora_cmd',
        'lora_downlink',
        'pi_link',
        'whereami_host',
        'zeroconf',
        'e22_serial',
        'cryptography.hazmat.primitives.ciphers.aead',
        'serial.tools.list_ports',
        'winotify',
        'winotify.audio',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='StratoPiGroundStation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)