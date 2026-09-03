# CLI reference

`c2mo2 --help` lists every subcommand; `c2mo2 <subcommand> --help` prints the options
below directly from argparse, so this file should never drift far from the real thing.
Options are grouped the way the commands are meant to be used, not the order they're
registered in.

- [Pipeline](#pipeline) - `create`: one command, a full instance
- [Instance lifecycle](#instance-lifecycle) - `add`, `remove`, `update`, `status`,
  `profile-instance`
- [Tools](#tools) - `tools list` / `install` / `remove` / `refresh`
- [Export](#export) - `wabbajack`
- [Low-level stages](#low-level-stages) - `fetch`, `report`, `download`, `inspect`,
  `install`, `profile`, `build`, `survey`

All paths accept forward or back slashes; quote any path with spaces.

## Pipeline

### `c2mo2 create`

Collection URL -> a runnable, self-contained MO2 instance, in one command. Runs
`fetch` -> (`survey`) -> `download` -> `inspect` -> `install` -> `profile` -> `build`
in-process, resumable (re-running skips finished stages), and writes the instance
ledger (`c2mo2-instance.json`).

```
c2mo2 create <url> --out OUT --game-path GAME_PATH [options]
```

| option | description |
| --- | --- |
| `--out OUT` | instance directory to create (**required**; keep it short) |
| `--game-path GAME_PATH` | the game install to build against (**required**) |
| `--revision REVISION` | revision number (default: latest) |
| `--stock-game` | copy the game into the instance and point MO2 at the copy (Wabbajack's "Stock Game" convention), so nothing ever patches the real install |
| `--reuse-downloads REUSE_DOWNLOADS` | an existing download store to hardlink/copy archives + `.meta` from first |
| `--jobs JOBS` | parallel workers per stage (default: 4) |
| `--resolution RESOLUTION` | profile display resolution: `auto`, `keep`, or `WxH` (default: `keep`) |
| `--vsync {on,off,keep}` | profile display vsync (default: `keep`) |
| `--window {fullscreen,borderless,windowed,keep}` | profile window mode (default: `keep`) |
| `--choices-overrides CHOICES_OVERRIDES` | JSON file of `{"<tag>": <Vortex choices object>}` for fresh-mode FOMODs |
| `--skip-survey` | skip the Nexus content-preview survey (it costs hourly API budget) |
| `--allow-missing` | carry on when Nexus no longer serves a file the collection pinned (the author deleted it); those mods are left out and listed in the summary. An md5 mismatch still stops the run. |
| `--mo2-version MO2_VERSION` | Mod Organizer 2 release to install (default: `2.5.2`) |
| `--rootbuilder-version ROOTBUILDER_VERSION` | Root Builder release to install (default: `5.1.1`) |

## Instance lifecycle

Everything here operates on an existing `create`d instance (`--instance <dir>`, which
must contain `c2mo2-instance.json`). An instance can hold several collections as
**layers**: `create` builds the base layer, `add` layers another collection on top
(sharing `mods/`/`downloads/`), `remove` takes one back off.

### `c2mo2 add`

Layer another collection onto an existing instance.

```
c2mo2 add <url> --instance INSTANCE [options]
```

| option | description |
| --- | --- |
| `--instance INSTANCE` | the instance directory to add it to (**required**) |
| `--revision REVISION` | revision number (default: latest) |
| `--jobs JOBS` | parallel workers per stage (default: 4) |
| `--game-path GAME_PATH` | the game install to install against (default: the instance ledger's) |
| `--choices-overrides CHOICES_OVERRIDES` | JSON file of FOMOD choice overrides for fresh-mode FOMODs |
| `--skip-survey` | skip the Nexus content-preview survey |
| `--allow-missing` | carry on when Nexus no longer serves a pinned file |
| `--reuse-downloads REUSE_DOWNLOADS` | an existing download store to hardlink/copy archives + `.meta` from first |

### `c2mo2 remove`

```
c2mo2 remove <slug> --instance INSTANCE [options]
```

| option | description |
| --- | --- |
| `slug` | the collection slug to remove, e.g. `xk05aw` (positional) |
| `--instance INSTANCE` | the instance directory (**required**) |
| `--purge-downloads` | also delete the layer's archives from `downloads/` (kept by default: harmless, and useful if you re-add the layer) |
| `--force` | allow removing the base layer the instance was created from |

Deletes only the folders that layer alone owns, reverts exactly the INI keys it set,
and re-renders the profile. Mods you installed by hand keep their place.

### `c2mo2 update`

Move a layer to a newer (or older) revision, applying only the delta: unchanged mods
cost nothing, changed mods are re-downloaded and reinstalled, removed mods are deleted
unless you've edited them by hand (in which case they're kept and reported).

```
c2mo2 update --instance INSTANCE [options]
```

| option | description |
| --- | --- |
| `--instance INSTANCE` | the instance directory (**required**) |
| `--layer LAYER` | the collection slug to update (default: the only layer, if there is one) |
| `--to TO` | target revision number, or `latest` (default: latest published) |
| `--dry-run` | print the plan and exit without touching anything |
| `--yes`, `-y` | apply the plan without asking (required when there is no terminal) |
| `--jobs JOBS` | parallel workers per stage (default: 4) |
| `--allow-missing` | carry on when Nexus no longer serves a file the new revision pins |
| `--purge-old` | delete the old revision's manifest folder too (kept by default, for diffing and for going back) |
| `--choices-overrides CHOICES_OVERRIDES` | JSON file of FOMOD choice overrides for fresh-mode FOMODs |

### `c2mo2 status`

Read-only view of an instance - never writes anything.

```
c2mo2 status --instance INSTANCE [--offline]
```

| option | description |
| --- | --- |
| `--instance INSTANCE` | the instance directory (**required**) |
| `--offline` | skip the Nexus lookups (no "latest revision" column) |

Lists each layer with its installed revision against the newest published one, how
many `mods/` folders belong to a collection, a tool, or you, the tools installed, and
whether the rendered profile still matches the ledger.

### `c2mo2 profile-instance`

Re-render an existing instance's profile from its ledger - e.g. to change display
settings without a full `add`/`update`.

```
c2mo2 profile-instance --instance INSTANCE [options]
```

| option | description |
| --- | --- |
| `--instance INSTANCE` | instance directory (has `c2mo2-instance.json`) (**required**) |
| `--resolution RESOLUTION` | profile display resolution: `auto`, `keep`, or `WxH` (default: `keep`) |
| `--vsync {on,off,keep}` | profile display vsync (default: `keep`) |
| `--window {fullscreen,borderless,windowed,keep}` | profile window mode (default: `keep`) |
| `--keep-inis` | do not touch existing profile INIs at all |
| `--forget-display` | clear the remembered `--resolution`/`--vsync`/`--window` choice and remove the generated override mod (if c2mo2 still owns it), then re-render |

A `--resolution`/`--vsync`/`--window` choice is remembered in the ledger and
re-applied on every later `add`/`remove`/`update`, refreshing the generated
"c2mo2 Display Settings" override mod that makes it stick even when a collection ships
SSE Display Tweaks. `--forget-display` clears that memory.

## Tools

`c2mo2 tools` installs optional modding tools from a built-in catalogue
(`tools_catalog.json`: xEdit, BethINI Pie, LOOT, NifSkope, Synthesis, Cathedral Assets
Optimizer, Pandora Behaviour Engine+, DynDOLOD + its Resources SE and DLL NG
companion mods) into an instance's `Tools\` folder and registers them as MO2
executables.

### `c2mo2 tools list`

```
c2mo2 tools list [--mo2-dir MO2_DIR]
```

| option | description |
| --- | --- |
| `--mo2-dir MO2_DIR` | portable MO2 instance directory (shown installed/not installed against it) |

### `c2mo2 tools install`

```
c2mo2 tools install [ids ...] --mo2-dir MO2_DIR [options]
```

| option | description |
| --- | --- |
| `ids` | tool ids to install, e.g. `xedit bethini-pie` (see `c2mo2 tools list`) |
| `--mo2-dir MO2_DIR` | portable MO2 instance directory (**required**) |
| `--all-default` | also install every catalogue entry with `default=true` |
| `--force` | reinstall even if the same version is already recorded as installed |

### `c2mo2 tools remove`

```
c2mo2 tools remove <ids ...> --mo2-dir MO2_DIR
```

Deletes `Tools\<id>`, drops its `[customExecutables]` entries, removes any companion
mods it solely owns, and re-renders the profile.

### `c2mo2 tools refresh`

```
c2mo2 tools refresh [ids ...] --mo2-dir MO2_DIR
```

Rewrites already-installed tools' `[customExecutables]` entries from the current
catalogue/`gamePath`, without re-downloading. Run this on instances built before
xEdit/DynDOLOD/TexGen learned to pass `-D:"<Stock Game>\Data"` - otherwise those tools
read the real Steam install's registry-derived Data folder instead of MO2's
VFS-managed one, and only see vanilla plugins.

## Export

### `c2mo2 wabbajack`

Compile an MO2 instance into a `.wabbajack` modlist with `wabbajack-cli`.

```
c2mo2 wabbajack --instance INSTANCE [options]
```

| option | description |
| --- | --- |
| `--instance INSTANCE` | the MO2 instance directory to compile (**required**) |
| `--name NAME` | modlist name (default: the base collection's) |
| `--version VERSION` | modlist version, .NET style (default: `<collection revision>.0.0`) |
| `--author AUTHOR` | modlist author (default: the curator) |
| `--description DESCRIPTION` | modlist description |
| `--website WEBSITE` | modlist website (default: the collection URL) |
| `--readme README` | modlist readme URL (default: the collection URL) |
| `--image IMAGE` | modlist image; a placeholder is generated if omitted |
| `--output OUTPUT` | output file (default: `<instance>/wabbajack/<name>.wabbajack`) |
| `--wabbajack-cli WABBAJACK_CLI` | path to `wabbajack-cli.exe` (default: auto-detect, else download the release) |
| `--dry-run` | write the settings file and print the pre-compile checklist, but do not compile |

The checklist (also shown for `--dry-run`) classifies every archive in `downloads/`
and flags anything that would have to be inlined into the `.wabbajack` instead of
resolved against an archive - large inlines, an instance path over 60 characters, a
missing Stock Game copy.

## Low-level stages

Each of these is one stage of the `create` pipeline, runnable standalone against
`work/<slug>/<revision>/` for development, debugging, or a custom workflow. Most
projects should just use `create`.

### `c2mo2 fetch`

Download a collection revision's manifest.

```
c2mo2 fetch <url> [options]
```

| option | description |
| --- | --- |
| `url` | collection URL (positional) |
| `--revision REVISION` | revision number (default: latest) |
| `--work WORK` | work directory (default: `./work`) |
| `--report` | print a summary after fetching |
| `--json` | summary as JSON |

### `c2mo2 report`

Summarise an already-fetched `collection.json` (mods, install modes, sources, phases,
mod rules, non-Nexus sources).

```
c2mo2 report <manifest> [--json]
```

### `c2mo2 download`

Download the Nexus-hosted mods a `collection.json` references.

```
c2mo2 download <manifest> [options]
```

| option | description |
| --- | --- |
| `manifest` | path to `collection.json` (positional) |
| `--out OUT` | output directory (default: `<manifest dir>/../downloads`) |
| `--jobs JOBS` | parallel download workers (default: 4) |
| `--limit LIMIT` | only download the first N mods |
| `--include-optional` | include optional mods (default) |
| `--no-optional` | skip mods marked optional |

Verifies MD5s against the manifest and writes MO2 `.meta` sidecars.

### `c2mo2 inspect`

Open each downloaded archive (via a bundled 7-Zip console binary) and record FOMOD
presence and layout.

```
c2mo2 inspect <downloads_json> [--out OUT] [--jobs JOBS]
```

### `c2mo2 install`

Build `mods/<Mod Name>/` for every mod: replay FOMOD choices, apply replicate file
lists, normalise layout, route game-root mods through Root Builder.

```
c2mo2 install <inspect_json> [options]
```

| option | description |
| --- | --- |
| `inspect_json` | path to `inspect.json` (positional) |
| `--manifest MANIFEST` | `collection.json` (default: from `downloads.json`) |
| `--mods-dir MODS_DIR` | output mods dir (default: `<rev>/mo2/mods`) |
| `--only ONLY` | install only mods whose name contains this |
| `--game-path GAME_PATH` | game install dir - its Data folder answers FOMOD `fileDependency` checks |
| `--jobs JOBS` | parallel installs (default: 4) |
| `--force` | reinstall mods that already exist |
| `--out OUT` | where to write `install.json` (default: `<mods-dir>/../install.json`) |
| `--owner OWNER` | who these mods belong to, e.g. `collection:<slug>@<rev>`; stamped into each mod's `meta.ini` for the instance ledger |
| `--choices-overrides CHOICES_OVERRIDES` | JSON file of FOMOD choice overrides for fresh-mode FOMODs |

Fresh-mode FOMODs (no recorded choices) take the installer's own defaults unless you
pass `--choices-overrides`.

### `c2mo2 profile`

Write the MO2 profile - mod order, plugins, `ModOrganizer.ini` - from `install.json`.

```
c2mo2 profile <install_json> [options]
```

| option | description |
| --- | --- |
| `install_json` | path to `install.json` written by the installer (positional) |
| `--mo2-dir MO2_DIR` | portable MO2 instance directory (default: the `install.json`'s directory) |
| `--profile-name PROFILE_NAME` | profile name (default: collection `info.name`, sanitised) |
| `--game-path GAME_PATH` | path to the game install, written to `ModOrganizer.ini` `gamePath` |
| `--separators` / `--no-separators` | insert phase separators into `modlist.txt` (default: insert) |
| `--mo2-version MO2_VERSION` | MO2 version string written to `ModOrganizer.ini` (default: `2.5.2`) |
| `--disable-optional` | write optional mods as disabled in `modlist.txt` (default: enabled) |
| `--resolution RESOLUTION` | `auto`, `keep`, or `WxH` (default: `keep`) |
| `--vsync {on,off,keep}` | (default: `keep`) |
| `--window {fullscreen,borderless,windowed,keep}` | (default: `keep`) |
| `--report-out REPORT_OUT` | where to write `profile-report.json` |
| `--owner OWNER` | who this profile belongs to; the INI keys written for it are recorded under that owner in the instance ledger |
| `--keep-inis` | do not touch existing profile INIs at all: skip seeding, collection INI tweaks, and display settings (for hand-edited INIs) |

### `c2mo2 build`

Lay down MO2 program files and Root Builder into a generated portable instance.

```
c2mo2 build <mo2_dir> [options]
```

| option | description |
| --- | --- |
| `mo2_dir` | portable MO2 instance directory (already has `mods/`, `profiles/`, `ModOrganizer.ini`) (positional) |
| `--game-path GAME_PATH` | path to the game install; rewrites `gamePath` (and related paths) in `ModOrganizer.ini` |
| `--mo2-version MO2_VERSION` | Mod Organizer 2 release to install (default: `2.5.2`) |
| `--rootbuilder-version ROOTBUILDER_VERSION` | Root Builder plugin release to install (default: `5.1.1`) |
| `--force` | re-extract MO2/Root Builder even if already present |
| `--stock-game` | copy `--game-path` into the instance and point MO2 at the copy, so collections that patch game files in place (e.g. a Runtime Swapper) never touch the real Steam install; requires `--game-path` |
| `--stock-game-dir STOCK_GAME_DIR` | where to copy the game to (default: `<mo2_dir>/Stock Game`) |
| `--force-stock` | re-copy the stock game even if the destination already looks populated |

### `c2mo2 survey`

Optional pre-flight: using Nexus's content previews, reports FOMOD presence and
archive layouts for a collection without downloading it (rate-limited, cached,
resumable).

```
c2mo2 survey <manifest> [options]
```

| option | description |
| --- | --- |
| `manifest` | path to `collection.json` (positional) |
| `--out OUT` | output path (default: `<manifest dir>/../survey.json`) |
| `--jobs JOBS` | parallel survey workers (default: 4) |
| `--all` | survey every Nexus mod (default: only mods without choices/hashes) |
| `--min-remaining MIN_REMAINING` | stop issuing v1 calls when hourly remaining drops below this (default: 100) |
| `--limit LIMIT` | only consider the first N targets |
