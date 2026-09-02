# PyInstaller spec for the c2wj-gui desktop wizard.
#
# Build with (from the repo root):
#   uv run pyinstaller packaging/c2wj-gui.spec
#
# Output goes to dist/c2wj-gui/. See packaging/README.md for the env var (C2WJ_DATA_DIR)
# that keeps the 7-Zip/MO2 download cache out of the frozen app's own folder.

import os

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
CATALOG = os.path.join(SRC_DIR, "collections2wabbajack", "tools_catalog.json")

a = Analysis(
    [os.path.join(REPO_ROOT, "packaging", "run_gui.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        (CATALOG, "collections2wabbajack"),
    ],
    hiddenimports=[
        "collections2wabbajack.gui.pages.signin",
        "collections2wabbajack.gui.pages.home",
        "collections2wabbajack.gui.pages.collection",
        "collections2wabbajack.gui.pages.location",
        "collections2wabbajack.gui.pages.tools_page",
        "collections2wabbajack.gui.pages.display",
        "collections2wabbajack.gui.pages.review",
        "collections2wabbajack.gui.pages.progress",
        "collections2wabbajack.gui.pages.manage",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="c2wj-gui",
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
    name="c2wj-gui",
)
