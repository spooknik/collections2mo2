#!/usr/bin/env bash
# Experimental alternative to the PyInstaller build: compile the GUI with Nuitka.
#
# Produces dist-nuitka/run_gui.dist/c2mo2-gui.exe (a folder build, like PyInstaller's).
# Needs the MSVC C++ toolchain (Visual Studio 2022 or the Build Tools); Nuitka finds it
# on its own and downloads its helper tools (clcache, depends.exe) on first use. The
# first build here took about five minutes. See packaging/README.md, "Nuitka build".
#
# Nuitka is not a project dependency; `uv run --with` puts it in a throwaway overlay.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(uv run --no-sync python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
# --include-distribution-metadata ships the *installed* metadata; after a version bump it is
# stale until `uv sync` runs, and the app would then report the old version in its title.
INSTALLED=$(uv run --no-sync python -c "from importlib.metadata import version; print(version('collections2mo2'))")
if [ "$INSTALLED" != "$VERSION" ]; then
  echo "installed collections2mo2 metadata is $INSTALLED but pyproject.toml says $VERSION; run 'uv sync' first" >&2
  exit 1
fi

uv run --no-sync --with nuitka --with ordered-set --with zstandard python -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --msvc=latest \
  --enable-plugin=pyside6 \
  --include-package=collections2mo2 \
  --include-package=keyring.backends \
  --include-distribution-metadata=collections2mo2 \
  --include-data-files=src/collections2mo2/tools_catalog.json=collections2mo2/tools_catalog.json \
  --include-data-dir=src/collections2mo2/gui/assets=collections2mo2/gui/assets \
  --windows-console-mode=disable \
  --windows-icon-from-ico=src/collections2mo2/gui/assets/icon.ico \
  --company-name=spooknik \
  --product-name=collections2mo2 \
  --file-version="$VERSION" \
  --product-version="$VERSION" \
  --file-description="collections2mo2 desktop wizard" \
  --copyright="Copyright (c) spooknik. GPL-3.0-or-later." \
  --output-dir=dist-nuitka \
  --output-filename=c2mo2-gui.exe \
  packaging/run_gui.py
