# PyInstaller spec for the c2mo2-gui desktop wizard.
#
# Build with (from the repo root):
#   uv run pyinstaller packaging/c2mo2-gui.spec
#
# Output goes to dist/c2mo2-gui/. See packaging/README.md for the env var (C2MO2_DATA_DIR)
# that keeps the 7-Zip/MO2 download cache out of the frozen app's own folder.

import os
import re
import sys
import tomllib

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
CATALOG = os.path.join(SRC_DIR, "collections2mo2", "tools_catalog.json")
ASSETS = os.path.join(SRC_DIR, "collections2mo2", "gui", "assets")
ICON = os.path.join(ASSETS, "icon.ico")

# Version resource. An exe with no VERSIONINFO block scores worse with Windows Defender's
# heuristics than one that names its product and publisher, so stamp the exe with the
# version from pyproject.toml (single source of truth; never edit the numbers here).
with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as fh:
    VERSION = tomllib.load(fh)["project"]["version"]

# `copy_metadata` below ships whatever dist-info is *installed*; after a version bump that
# is stale until `uv sync` runs, and the frozen app would then report the old version.
from importlib.metadata import version as _installed_version  # noqa: E402

if _installed_version("collections2mo2") != VERSION:
    raise SystemExit(
        f"installed collections2mo2 metadata is {_installed_version('collections2mo2')} but "
        f"pyproject.toml says {VERSION}; run `uv sync` before building"
    )


def _version_tuple(text):
    """'0.1.0' -> (0, 1, 0, 0); a pre-release suffix like '0.1.0rc1' keeps the numeric part."""
    parts = [int(n) for n in re.findall(r"\d+", text)[:4]]
    return tuple(parts + [0] * (4 - len(parts)))


VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_version_tuple(VERSION),
        prodvers=_version_tuple(VERSION),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,  # VOS_NT_WINDOWS32
        fileType=0x1,  # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # US English, Unicode
                    [
                        StringStruct("CompanyName", "spooknik"),
                        StringStruct("FileDescription", "collections2mo2 desktop wizard"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "c2mo2-gui"),
                        StringStruct(
                            "LegalCopyright",
                            "Copyright (c) spooknik. Licensed under the GPL-3.0-or-later.",
                        ),
                        StringStruct("OriginalFilename", "c2mo2-gui.exe"),
                        StringStruct("ProductName", "collections2mo2"),
                        StringStruct("ProductVersion", VERSION),
                        StringStruct("Comments", "https://github.com/spooknik/collections2mo2"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
    ],
)

a = Analysis(
    [os.path.join(REPO_ROOT, "packaging", "run_gui.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        (CATALOG, "collections2mo2"),
        (ASSETS, os.path.join("collections2mo2", "gui", "assets")),
        # The package's dist-info, so importlib.metadata resolves `__version__` in the
        # frozen app (window title, Nexus User-Agent) instead of "0.0.0+unknown".
        *copy_metadata("collections2mo2"),
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

# -- OpenSSL: ship Python's own copy, never one found on PATH ---------------------------
# PySide6's hook collects Qt's TLS plugins, and qopensslbackend.dll names OpenSSL by file
# name (libcrypto-3-x64.dll / libssl-3-x64.dll on 64-bit). PyInstaller resolves those
# names through PATH, and a Git Bash shell has Git's own OpenSSL (a different version) on
# PATH. When Python's _ssl.pyd uses the same file names (CPython 3.13 does; 3.12 says
# libcrypto-3.dll), the Git copy wins the name clash and the frozen app cannot import
# ssl at all ("The specified procedure could not be found"). Seen 2026-09-03 with uv's
# CPython 3.13.13 + Git 2.x. The app never uses Qt networking, so the OpenSSL plugin is
# dropped (Qt keeps its Schannel backend), and any OpenSSL DLL PyInstaller picked up from
# outside the running interpreter is swapped for the interpreter's own file.
_openssl_names = re.compile(r"^lib(crypto|ssl)-3[-\w]*\.dll$", re.IGNORECASE)
_python_dlls = os.path.join(sys.base_prefix, "DLLs")
_fixed = []
for dest, src, kind in a.binaries:
    base = os.path.basename(dest)
    if base.lower() == "qopensslbackend.dll":
        print(f"spec: dropping Qt OpenSSL TLS plugin {dest}")
        continue
    if _openssl_names.match(base):
        own = os.path.join(_python_dlls, base)
        if not os.path.abspath(src).lower().startswith(os.path.abspath(sys.base_prefix).lower()):
            if not os.path.exists(own):
                raise SystemExit(
                    f"{base} was resolved from {src}, outside the Python install, and "
                    f"{own} does not exist to replace it"
                )
            print(f"spec: {base}: using {own} instead of {src}")
            src = own
    _fixed.append((dest, src, kind))
a.binaries = type(a.binaries)(_fixed)

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
    version=VERSION_INFO,
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
