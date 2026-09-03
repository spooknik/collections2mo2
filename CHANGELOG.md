# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed

- Windows Defender flagged the 0.1.0 `c2mo2-gui.exe` as a virus. The release workflow now
  compiles PyInstaller's bootloader from source instead of shipping the prebuilt stub that
  antivirus vendors flag by hash, and the exe carries a version resource (publisher,
  product, version). The FAQ explains the false positive, how to verify a download against
  the release's SHA-256 digest, and how to run from source instead.

## 0.1.0 - 2026-09-03

First public release. `collections2mo2` (`c2mo2`) turns a Nexus Mods collection manifest
into a portable Mod Organizer 2 instance, with an optional `.wabbajack` export at the end.

This project was previously named `collections2wabbajack` (CLI `c2wj`) and has been
renamed to `collections2mo2` (CLI `c2mo2`) to reflect that a working MO2 instance is the
main output and Wabbajack export is an optional last step. Instances built under the old
name are migrated automatically the first time you open them with `c2mo2` - the ledger,
stage files, build record and generated display-settings mod are renamed in place, and the
old environment variable, data folder, keyring entry and GUI recent-instances list are still
read as a fallback.

### Added

- Full pipeline (`fetch`, `download`, `inspect`, `install`, `profile`, `build`) that turns a
  collection's manifest into a self-contained, portable MO2 instance.
- `create`, a one-step, resumable command that runs the whole pipeline, with
  `--reuse-downloads` and `--stock-game` options.
- `--stock-game`, which copies the game into an isolated `Stock Game` folder inside the
  instance so nothing a collection installs ever touches your real Steam install.
- Collection layering: `c2mo2 add` and `c2mo2 remove` stack more than one collection on a
  single instance, sharing mods and downloads between layers.
- `c2mo2 update`, which moves a layer to a newer published revision by diffing the two
  manifests and re-downloading/reinstalling only what changed, with `--dry-run`, `--yes`,
  and a view of the curator's changelog for the revision.
- `c2mo2 status`, a read-only summary of every layer's installed vs. latest revision.
- `c2mo2 tools`, a catalogue-driven installer for optional modding tools (xEdit, BethINI
  Pie, LOOT, NifSkope, Synthesis, Cathedral Assets Optimizer, Pandora Behaviour Engine+,
  DynDOLOD), plus `tools remove` and `tools refresh`.
- `c2mo2 wabbajack`, which compiles a finished instance into a `.wabbajack` modlist via
  `wabbajack-cli`, after a pre-compile checklist (download metadata coverage, inlined
  size, Stock Game setup).
- Display settings support (`--resolution`, `--vsync`, `--window`): for collections that
  ship SSE Display Tweaks, generates a small override mod and remembers your choice so
  later `add`/`remove`/`update` runs keep applying it instead of reverting silently.
- `c2mo2-gui`, a guided PySide6 wizard: paste-a-key Nexus sign-in (stored in Windows
  Credential Manager), Steam game auto-detection, an optional-tools checklist, a display
  settings page, a review step, live per-file progress with rate/ETA, and a Manage tab for
  updating, layering onto, or exporting an existing instance.

- `create --tools <id ...>` installs catalogue tools as the pipeline's last stage; the GUI
  wizard's Tools page now actually does this (previously the selection was shown on the
  review page but never installed).

- "Remove instance..." on the GUI's Manage tab, which deletes a whole instance folder
  (MO2, the Stock Game copy, mods, downloads and installed tools) after a confirmation
  that names the folder and its total size, then forgets it from the recent list.
- A guard on the Manage tab against removing the base collection - that is CLI-only
  (`c2mo2 remove --force`) - and a way back for an instance that has no collection left:
  "Set up a collection here" runs the normal create wizard pinned to that folder, so its
  existing downloads are reused.

- An app icon for the GUI window, taskbar and packaged executable
  (`scripts/make_icon.py` regenerates it).
- The script extender (SKSE, SKSE VR, F4SE, NVSE) is found by its loader executable in
  the installed mod folders, not just by mod name, and is pinned as MO2's default
  executable in a fresh instance.
- A game version check: the wizard's location page and `c2mo2 create` read the game
  executable's own version and compare it with the version the collection was built
  against (`info.gameVersions`), confirming a match on one line and warning on a
  difference. Advisory only - it never blocks a run, because a `Stock Game` copy can be
  downgraded or patched afterwards. `c2mo2 report` prints the collection's target
  version too.
- More instance-folder location warnings: Program Files, the Windows folder, the
  Desktop (including OneDrive's), a Steam library (`steamapps`) and the game folder
  itself, alongside the existing long-path, OneDrive and Documents warnings. `create`
  now prints the same list the wizard shows.

### Changed
- The wizard's "Check FOMODs" pre-flight button is gone. It spent one Nexus API call per
  mod from the same hourly budget the downloads need, and the install log reports the
  same thing (which mods used installer defaults). `c2mo2 survey` stays on the CLI.
- The profile summary now says "N skipped (reference mods not in this collection)" for
  curator rules that reference mods outside the collection, instead of "ignored
  (unresolvable)"; those rules were and are harmless.

- Renamed the project from `collections2wabbajack`/`c2wj` to `collections2mo2`/`c2mo2` -
  see the migration note above.
- FOMOD installs now always honour the curator's recorded choices; `fileDependency` checks
  are resolved from the manifest's plugin list, your game's Data folder, and mods already
  installed in the same run, instead of being guessed at.
- xEdit-family tools (xEdit, TexGen, DynDOLOD) now point explicitly at the instance's Stock
  Game copy so they see your installed mods instead of the real Steam install's vanilla
  Data folder.
- Mod folder names are capped at 80 characters and deduplicated, so very long curator mod
  names no longer risk exceeding Windows' path-length limit.
- `profile` now seeds profile-local game INIs from your own `My Games` INIs and merges in
  the collection's INI Tweaks, and correctly patches duplicate INI sections.
- Direct-URL download sources (e.g. GitHub releases) are now downloaded, MD5-verified, and
  given proper metadata so both MO2 and Wabbajack recognise them.

### Fixed

- `ModOrganizer.ini` is now written in MO2's own format and the instance is marked
  portable, so re-running `profile` after `build` no longer makes MO2 think the instance
  is missing.
- The exported `.wabbajack` checklist now correctly traces tool binaries and tool-owned
  mods back to their downloads instead of assuming they should be bundled inline.
- GUI progress bar, elapsed timer, and busy-state handling during long-running operations.
