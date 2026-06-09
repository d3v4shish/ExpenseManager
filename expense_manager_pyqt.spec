# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

ROOT = Path(SPECPATH).resolve()
block_cipher = None

datas = [
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "src" / "expenses" / "defaults"), "src/expenses/defaults"),
    (str(ROOT / "src" / "expenses" / "ui" / "cards"), "src/expenses/ui/cards"),
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    name="expense_manager_pyqt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=str(ROOT / "assets" / "icons" / "expense_manager_matte.ico"),
    disable_windowed_traceback=False,
)
