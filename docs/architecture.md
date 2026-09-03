# Architecture and technical notes

Technical reference for contributors: what the tool is, how the pipeline fits together, and
the non-obvious facts about Nexus collections and MO2 that the code relies on.

## What this is

A standalone Python CLI (`c2mo2`) that turns a Nexus Mods collection (the Vortex manifest) into a
Mod Organizer 2 portable instance, with an optional Wabbajack compile at the end. It replaces
the earlier attempt to do this as an MO2 Python plugin; MO2's plugin API cannot do file-level
installs or write profiles. Pipeline: `fetch` -> `download` -> `inspect` -> `install` -> `profile`
-> `build` (-> `tools` when `--tools`/the GUI Tools page asks for catalogue tools; it runs last
because `tools install` writes the ledger itself); `create` runs all of them into one instance dir (`<out>/c2mo2/` holds the stage JSON,
`<out>/downloads/` the archives) and writes the ledger `c2mo2-instance.json` (`ledger.py`).
An instance can hold several collections as layers: `create` = init + `add` the first layer +
`build`, and `c2mo2 add` / `c2mo2 remove` (`layers.py`) put further collections on and off, sharing
`mods/` and `downloads/` while each keeps its own `c2mo2/<slug>-<rev>.*.json`. The profile is
rendered from every layer at once by `profile.render_instance`. `c2mo2 update` (`update.py`) moves
one layer to a newer revision by diffing the two manifests and applying only the delta;
`c2mo2 status` is its read-only companion.
Stage commands used standalone read/write `work/<slug>/<revision>/`. `tools` installs optional
modding tools from `tools_catalog.json`. Progress goes through `reporter.Reporter` so a GUI can
hook in. Tests: `uv run pytest -q` (`tests/`, `local` marker for tests needing `tools/7za.exe`).

## Commands

```
uv sync                       # Python >= 3.12
uv run c2mo2 --help
uvx ruff check src            # lint (ruff config in pyproject.toml, line length 100)
```

Tests: `uv run pytest -q` (`tests/`); end-to-end verification is running the pipeline against
the development collection `h2uqa3` (see README) and spot-checking `work/`. `work/`, `tools/` and `.env` are
gitignored. Never print `.env` contents or the API key.

## Non-obvious facts (verified against the live API, Sept 2026)

- Collection metadata is readable anonymously via GraphQL, but the manifest archive needs an
  `apikey` header: GraphQL `collectionRevision.downloadLink` returns a path like
  `/v2/collections/<id>/revisions/<id>/download_link`; GET on it returns
  `{"download_links": [{"name","short_name","URI"}]}`. The archive is 7z with `collection.json`.
- Mod file downloads use the v1 REST API and require Nexus Premium. Every manifest mod carries a
  pinned `fileId` + `md5`; we always download the pinned file even for `updatePolicy: latest`.
  Many pinned files sit in Nexus's "old version"/"archived" categories; that is normal.
- Manifest install modes: `hashes` present = Vortex "Replicate" (exact file list + optional
  `patches`); `choices` present = FOMOD with recorded answers; neither = fresh install.
- **FOMOD choice replay is positional.** Vortex records one `options[]` entry per `installStep` in
  XML order, including steps that were never visible (empty `choices`). Match by index, verify names,
  evaluate `visible` yourself so hidden steps set no flags.
- Only rules whose both endpoints resolve (by `fileMD5` to a mod's `source.md5`) matter; most
  `modRules` reference mods the curator has but did not include. Ordering = phase, manifest order,
  then those rules.
- `details.type == "dinput"` marks game-root mods (SKSE, Engine Fixes part 2, DLL shims). We map
  them to Root Builder's `Root/` folder. Some archives ship `vortex_override_instructions.json`
  with an explicit copy list; honour it when present.
- Layout normalisation mirrors MO2's simple installer. A single top-level folder is only unwrapped
  when descending actually ends on Data-like content; otherwise the root is installed as-is
  (`Nemesis_Engine`, `MapMarkers`, `Shaders` are Data content, not wrappers). `__MACOSX` and `._*`
  are junk. For `dinput` mods only plugins/BSAs count as Data evidence; loose DLL/EXE/INI/JSON files
  go under `Root/` (Runtime Swapper, SKSE loader, preloaders).
- FOMOD `fileDependency` checks are answered from the manifest `plugins` list, the game's Data
  folder (`--game-path`) and mods already installed in the run; recorded curator picks are always
  installed regardless of how a plugin's type resolves. Verified on GTS: every plugin in the
  curator's load order is produced by the install (2,170 of 2,170).
- Mod folder names are capped at 80 characters with a hash suffix (Windows MAX_PATH; the instance
  path, folder and nested mod files share the 260 budget) and deduplicated across the manifest
  (`naming.assign_folder_names`). Install the instance to a short path (e.g. `D:\GTS`).
- **Vortex re-issues `source.tag` on every revision.** h2uqa3 66 -> 68 shares not one tag out of
  287/292, so `update` pairs mods by `(modId, fileId)`, then `md5`, then `modId` alone; `tag` is
  tried first only in case a curator's tooling keeps it. `profile.py` already matches install
  entries to manifest mods by md5/`(modId, fileId)` for the same reason.
- `CollectionRevision.collectionChangelog` (`{revisionNumber, description, createdAt}`) is the
  curator's per-revision note; `Collection.revisions` lists every revision with its
  `revisionStatus`. Both are anonymous-readable like the rest of the GraphQL surface.
- `c2mo2 update` only reinstalls a mod when its file, `choices` or `hashes` changed; a `phase` or
  `optional` flip is a profile re-render and a pure rename is a folder rename. A removed mod whose
  folder has *more* files than `install.json` recorded, or files newer than the install, is kept
  and reported rather than deleted (`update.looks_user_modified`) -- extraction restores archive
  mtimes, so an untouched mod folder is always older than its install.
- `c2mo2 install --only X --force` reinstalls a subset and merges into the existing `install.json`;
  a full `--force` run takes ~15 minutes for GTS. Close MO2 on that instance first.
- 7-Zip is bootstrapped into `tools/` from the official GitHub release assets because py7zr cannot
  decode BCJ2 and the "extra" package lacks the RAR codec (see `sevenzip.py` docstring).
- MO2's executables dropdown has a hidden `<Edit...>` item at index 0, so
  `[Widgets] MainWindow_executablesListBox_index=1` in ModOrganizer.ini selects the *first*
  `[customExecutables]` entry, and MO2 falls back to 1 when the key is missing. We write the
  script extender first and pin the key (`profile.render_mo2_ini`); an INI MO2 has touched is
  only topped up, never rewritten, so the user's own choice survives re-renders.
- The game version a collection targets is in the manifest as `info.gameVersions`
  (`["1.6.1170.0"]`) and on GraphQL as `collectionRevision { gameVersions { reference } }`
  (verified live, both anonymous-readable). `game_version.py` compares it with the main exe's
  Windows version resource (`GetFileVersionInfoSizeW`/`VerQueryValueW` on the root block, via
  `ctypes`), treating the first three numeric components as significant -- Steam and Nexus
  disagree on the fourth routinely. The check is advisory everywhere: `create` warns and
  carries on, the wizard never disables Continue, because a `Stock Game` copy can be
  downgraded or patched after the build.
- Instance-location warnings live in `create.instance_path_warnings` and `api.path_warnings`
  delegates to it (api imports create, not the other way round), so `create` and the wizard
  show the same text.
- PyInstaller's prebuilt bootloader stub is what Windows Defender flags (generic
  `Wacatac`/`Wacapew` names). It ships in the sdist too, so `--no-binary` alone changes nothing;
  `PYINSTALLER_COMPILE_BOOTLOADER=1` forces a `waf` compile with MSVC (~20 s). `release.yml` sets
  that plus `UV_NO_BINARY_PACKAGE=pyinstaller` and cleans uv's pyinstaller cache first; the spec
  also stamps a `VSVersionInfo` resource from the pyproject version. See packaging/README.md.
- A `--resolution`/`--vsync`/`--window` choice is remembered in the ledger's `display` key and
  re-applied whenever a render is given `keep` for that field (`profile._resolve_effective_display`),
  so `add`/`remove`/`update` -- which never pass these flags -- keep refreshing the generated SSE
  Display Tweaks override mod instead of silently reverting to the collection's own settings;
  `profile-instance --forget-display` clears it.

## Shared contracts

- `naming.mod_folder_name(mod)` is the single source of truth for MO2 mod folder names; the
  installer and profile writer must agree.
- `downloads.json` -> `inspect.json` -> `install.json` schemas are documented in the module
  docstrings of `downloader.py`, `archive_inspect.py`, `installer.py`.
