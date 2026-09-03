# Third-party software

This project bundles some third-party Python packages and, at run time, downloads other
third-party tools it does not redistribute. This file lists both, and is the single
visible place where the versions those runtime downloads are pinned to are recorded.

## Bundled: Python runtime dependencies

These packages are installed alongside `collections2mo2` (via `uv sync` from source, or
inside the packaged GUI build) and their code ships with it. Licence text for each is in
its own package metadata (`site-packages/<package>-*/LICENSE` or similar) or upstream
repository.

| Package | Version | Licence | Verified via |
| --- | --- | --- | --- |
| requests | 2.34.2 | Apache-2.0 | `importlib.metadata.metadata("requests")["License"]` |
| python-dotenv | 1.2.3 | BSD-3-Clause | `importlib.metadata.metadata("python-dotenv")["License"]` |
| py7zr | 1.1.3 | LGPL-2.1-or-later | `importlib.metadata.metadata("py7zr")["License-Expression"]` |
| PySide6 | 6.11.2 | LGPL-3.0-only (or GPL-2.0/3.0) | `importlib.metadata.metadata("PySide6")["License"]` |
| keyring | 25.7.0 | MIT | `importlib.metadata.metadata("keyring")["License-Expression"]` |

Versions and licence identifiers above were read directly from the installed packages'
metadata (`uv run python -c "from importlib.metadata import metadata; ..."`), not from
memory, against the environment locked in `uv.lock` for this release.

**PySide6 / Qt (LGPL-3.0-only) note:** the GUI (`c2mo2-gui`) links against PySide6 and,
in the packaged (PyInstaller) release build, ships its Qt shared libraries alongside the
executable rather than statically linking them. This satisfies the LGPL's relinking
requirement - the bundled `.dll`/`.pyd` files can be replaced with a compatible build of
PySide6/Qt without rebuilding the application. Running from source (`uv sync` + `uv run
c2mo2-gui`) uses your own `uv`-installed copy of PySide6 the same way.

## Downloaded at runtime, not redistributed

`collections2mo2` does not ship these tools in its own package or installer. It downloads
them from their official sources the first time they're needed and runs them as separate
processes; nothing from them is compiled into or copied out of this project's own code.

- **Mod Organizer 2** - downloaded from the official
  [ModOrganizer2/modorganizer](https://github.com/ModOrganizer2/modorganizer) GitHub
  releases by `c2mo2 build`. Pinned version: `2.5.2` (`DEFAULT_MO2_VERSION` in
  `src/collections2mo2/build.py`). GPL-3.0.
- **Root Builder** - the Root Builder MO2 plugin, downloaded from its GitHub releases by
  `c2mo2 build` and installed into the instance's `plugins/` folder. Pinned version:
  `5.1.1` (`DEFAULT_ROOTBUILDER_VERSION` in `src/collections2mo2/build.py`), by Kezyma.
- **7-Zip 26.02** - a standalone 7-Zip console binary, bootstrapped into `tools/` (never
  installed system-wide) from the official
  [ip7z/7zip](https://github.com/ip7z/7zip) GitHub release assets, and used as a
  subprocess to list and extract mod archives (see `src/collections2mo2/sevenzip.py` for
  why three separate downloads are needed to assemble a RAR-capable build). 7-Zip's own
  code is LGPL-2.1-or-later; the RAR/RAR5 decoder it includes carries the separate unRAR
  licence restriction (it may be used to unpack RAR archives, but not to reverse-engineer
  or reimplement the RAR compression algorithm).
- **Wabbajack CLI** (`wabbajack-cli.exe`) - located via an existing Wabbajack install (the
  `wabbajack:` protocol handler) or, failing that, downloaded from the latest release of
  [wabbajack-tools/wabbajack](https://github.com/wabbajack-tools/wabbajack) on GitHub by
  `c2mo2 wabbajack` (see `src/collections2mo2/wabbajack.py`). Not version-pinned by this
  project - it always resolves to whatever GitHub currently serves as that repository's
  latest release. GPL-3.0.
- **Optional modding tools** (`c2mo2 tools`) - installed only if you choose to, from the
  catalogue in `src/collections2mo2/tools_catalog.json`. Each entry's `verified` date is
  when its source and layout were last checked against the catalogue:
  - `xedit` - xEdit (SSEEdit), from
    [TES5Edit/TES5Edit](https://github.com/TES5Edit/TES5Edit) (GitHub releases).
    Verified 2026-09-02.
  - `bethini-pie` - BethINI Pie, from its author's Nexus Mods "site" page (mod 631).
    Verified 2026-09-02.
  - `loot` - LOOT, from [loot/loot](https://github.com/loot/loot) (GitHub releases).
    Verified 2026-09-02.
  - `nifskope` - NifSkope, from
    [niftools/nifskope](https://github.com/niftools/nifskope) (GitHub releases).
    Verified 2026-09-02.
  - `synthesis` - Synthesis, from
    [Mutagen-Modding/Synthesis](https://github.com/Mutagen-Modding/Synthesis) (GitHub
    releases). Verified 2026-09-02.
  - `cao` - Cathedral Assets Optimizer, from Nexus Mods (Skyrim Special Edition, mod
    23316). Verified 2026-09-02.
  - `pandora` - Pandora Behaviour Engine+, from
    [Monitor221hz/Pandora-Behaviour-Engine-Plus](https://github.com/Monitor221hz/Pandora-Behaviour-Engine-Plus)
    (GitHub releases). Verified 2026-09-02.
  - `dyndolod` - DynDOLOD, from Nexus Mods (Skyrim Special Edition, mod 68518), by
    sheson. Verified 2026-09-02.
  - `dyndolod-resources` (companion mod) - DynDOLOD Resources SE, from Nexus Mods
    (Skyrim Special Edition, mod 52897). Verified 2026-09-02.
  - `dyndolod-dll-ng` (companion mod) - DynDOLOD DLL NG and Scripts, from Nexus Mods
    (Skyrim Special Edition, mod 97720). Verified 2026-09-02.

  Each tool's own licence note is recorded next to it in `tools_catalog.json`
  (`license_note`); several are GPL-3.0, others are author-distributed freeware with no
  redistribution permitted, which is exactly what "downloaded, not redistributed" means
  here - `collections2mo2` fetches them from their own official sources on your machine,
  it never mirrors or repackages them itself.
