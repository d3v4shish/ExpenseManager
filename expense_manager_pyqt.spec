# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

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
    hiddenimports=[
        "src.expenses.ui.analysis_widget",
        "src.expenses.ui.config_widget",
        "src.expenses.ui.debug_widget",
        "src.expenses.ui.insights_widget",
        "src.expenses.ui.overview_widget",
        "src.expenses.ui.screen_api",
        "src.expenses.ui.settings_widget",
        "src.expenses.ui.sources_widget",
        "src.expenses.sources.registry",
        "src.expenses.sources.providers",
        "src.expenses.sources.parsers",
        "src.app.cli",
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
# ExpenseManager does not accept TIFF input. Excluding the optional plugin avoids
# shipping a plugin with a host-specific libtiff dependency on Linux.
a.binaries = [item for item in a.binaries if Path(item[0]).name != "libqtiff.so"]
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ExpenseManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    hide_console="hide-early" if sys.platform == "win32" else None,
    icon=str(ROOT / "assets" / "icons" / ("expense_manager_matte.ico" if sys.platform == "win32" else "expense_manager_matte.png")),
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ExpenseManager",
)
