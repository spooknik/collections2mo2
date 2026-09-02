# Packaging `c2wj-gui`

Builds a standalone `dist/c2wj-gui/c2wj-gui.exe` with PyInstaller (a dev dependency;
`uv sync` installs it).

```
uv run pyinstaller packaging/c2wj-gui.spec
```

Output: `dist/c2wj-gui/` (one folder, not a single-file exe -- faster startup, and it's
what the spec's `COLLECT` step produces). `tools_catalog.json` is bundled into it, next
to the frozen `collections2wabbajack` package, via the spec's `datas` entry.

## `C2WJ_DATA_DIR`

`sevenzip.TOOLS_DIR` and `build.CACHE_DIR` / `tools.CACHE_DIR` are computed at import
time as "two directories above the module" (`Path(__file__).resolve().parents[2]`) --
that is the repo root under `uv run`, but under a frozen PyInstaller build it resolves
into the temporary `_MEI...` extraction directory, which is deleted after every run.
Left alone, that means 7-Zip and the cached MO2/Root Builder downloads would be
re-fetched (several hundred MB) on every single launch of the packaged app.

`api.py` (`_apply_data_dir_override`, run at import time, before any engine call) works
around this **without editing `sevenzip.py` / `build.py` / `tools.py`**: those three
modules read `TOOLS_DIR` / `CACHE_DIR` as a plain global at call time (verified by
reading them), so reassigning the module attribute after import takes effect. The
frozen app (`getattr(sys, "frozen", False)`, which PyInstaller sets) is redirected to
`%LOCALAPPDATA%\collections2wabbajack\tools\` automatically; set `C2WJ_DATA_DIR` to
override that location explicitly (also works for `uv run c2wj-gui`, e.g. to point two
installs at a shared cache).

## Build status (last verified: 2026-09-02)

Built cleanly: `dist\c2wj-gui\c2wj-gui.exe` plus `dist\c2wj-gui\_internal\` (includes
`_internal\collections2wabbajack\tools_catalog.json`, confirming the `datas` entry
worked). PySide6's hook has to walk and copy the full PySide6/Qt tree
(`pyside6-essentials` + `pyside6-addons` are ~230 MB downloaded, and the analysed/copied
set under PyInstaller is larger again), so the build itself takes several minutes --
that is normal, not a hang.

Smoke-tested by launching `dist\c2wj-gui\c2wj-gui.exe` directly: the process starts and
stays up (~90 MB resident) with no immediate crash. The full wizard flow was not
re-verified against the frozen build (only against `uv run c2wj-gui`) -- do that before
shipping a release build.

If the build itself fails, the most likely causes for this project specifically are:

- **py7zr's C extensions** (brotli, zstandard, pyppmd, pybcj, pyzstd, ...) sometimes need
  an explicit `--collect-all py7zr` or per-package `--collect-all` if PyInstaller's
  hooks miss a compiled dependency; add `collect_all("py7zr")`-style entries to the spec
  if `ModuleNotFoundError` shows up for one of those at runtime.
- **`keyring`'s backend discovery** is entry-points-based (`importlib.metadata`); if the
  frozen exe can't find Windows Credential Manager, add
  `hiddenimports=["keyring.backends.Windows"]` (already common with PyInstaller +
  keyring; not yet needed here since no frozen run has been exercised).
