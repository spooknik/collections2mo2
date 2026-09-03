# PyInstaller spec for the c2mo2-gui desktop wizard.
#
# Build with (from the repo root):
#   uv run pyinstaller packaging/c2mo2-gui.spec
#
# Output goes to dist/c2mo2-gui/. See packaging/README.md for the env var (C2MO2_DATA_DIR)
# that keeps the 7-Zip/MO2 download cache out of the frozen app's own folder.

import os

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
CATALOG = os.path.join(SRC_DIR, "collections2mo2", "tools_catalog.json")
ASSETS = os.path.join(SRC_DIR, "collections2mo2", "gui", "assets")
ICON = os.path.join(ASSETS, "icon.ico")

a = Analysis(
    [os.path.join(REPO_ROOT, "packaging", "run_gui.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        (CATALOG, "collections2mo2"),
        (ASSETS, os.path.join("collections2mo2", "gui", "assets")),
    ],
    hiddenimports=[
        "collections2mo2.gui.pages.signin",
        "collections2mo2.gui.pages.home",
        "collections2mo2.gui.pages.collection",
        "collections2mo2.gui.pages.location",
        "collections2mo2.gui.pages.tools_page",
        "collections2mo2.gui.pages.display",
        "collections2mo2.gui.pages.review",
        "collections2mo2.gui.pages.progress",
        "collections2mo2.gui.pages.manage",
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
    name="c2mo2-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
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
    name="c2mo2-gui",
)
