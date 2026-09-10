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
`<out>/downloads/` the archives by default) and writes the ledger `c2mo2-instance.json` (`ledger.py`).
An instance can hold several collections as layers: `create` = init + `add` the first layer +
`build`, and `c2mo2 add` / `c2mo2 remove` (`layers.py`) put further collections on and off, sharing
`mods/` and `downloads/` while each keeps its own `c2mo2/<slug>-<rev>.*.json` (manifest,
downloads, inspect, install, and `categories.json` from `categories.prepare_layer`). The profile is
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

- Collection metadata is readable anonymously via GraphQL, but the manifest archive needs a
  sign-in (a Bearer token or, for developers, the `apikey` header): GraphQL
  `collectionRevision.downloadLink` returns a path like
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
- `sevenzip.extract` tolerates 7-Zip exit 2 when every `ERROR:` line is about reapplying a
  reparse point or symbolic link *and* every file in the listing is present in the output at
  its recorded size; the lines come back as warnings the installer stores on the entry.
  Zips made from a OneDrive/Dropbox folder carry `FILE_ATTRIBUTE_REPARSE_POINT` (attribute
  `L` in `7za l -slt`) on ordinary files -- the cloud placeholder -- and 7-Zip writes the
  bytes, then fails with "Incorrect reparse stream"; no `-snl` variant avoids it. Seen on
  Race-Based Textures (xa2h3u rev 33, 2026-09-08). CRC/data errors still raise.
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
- Releases ship the Nuitka build only (`packaging/build-nuitka.sh`; `release.yml` moves
  `dist-nuitka/run_gui.dist/` to `dist/c2mo2-gui/` and zips it as `c2mo2-gui-<tag>-windows-x64.zip`,
  since 0.1.3). PyInstaller's prebuilt bootloader stub is what Windows Defender flags (generic
  `Wacatac`/`Wacapew` names); compiling the bootloader ourselves (`PYINSTALLER_COMPILE_BOOTLOADER=1`,
  releases 0.1.1 to 0.1.3-rc1) reduced but did not stop the cloud detections, while the Nuitka zip
  shipped next to it in 0.1.3-rc1 was never flagged. The spec stays for local builds. See
  packaging/README.md.
- `sevenzip.ensure_7za` is called from parallel workers; its bootstrap is serialised with a
  module lock and `cmd_inspect` calls it once before its pool (the first frozen GTS run lost the
  first 18 archives to four concurrent bootstraps). All child processes pass
  `sevenzip.NO_WINDOW` (`CREATE_NO_WINDOW`) because the windowed exe otherwise flashes consoles.
- A `--resolution`/`--vsync`/`--window` choice is remembered in the ledger's `display` key and
  re-applied whenever a render is given `keep` for that field (`profile._resolve_effective_display`),
  so `add`/`remove`/`update` -- which never pass these flags -- keep refreshing the generated SSE
  Display Tweaks override mod instead of silently reverting to the collection's own settings;
  `profile-instance --forget-display` clears it.

- `tools install` skips a companion mod (DynDOLOD Resources SE / DLL NG) when any layer's
  manifest pins the same Nexus `modId` (`tools._collection_nexus_mods`), and removes a copy an
  earlier run installed. Companion mods render *above* every collection block, so the
  catalogue's newest-main copies overrode GTS's pinned ones and DynDOLOD's DLL rejected the
  scripts at game start (2026-09-03). Re-running the tool install repairs such an instance.

- The PyInstaller spec swaps any OpenSSL DLL resolved from outside `sys.base_prefix` for the
  interpreter's own and drops Qt's `qopensslbackend.dll`: in a Git Bash shell PATH has Git's
  OpenSSL under the same `libcrypto-3-x64.dll` name CPython 3.13 uses, and the frozen `_ssl`
  then fails ("procedure could not be found"), so the GUI opens on Sign-in with an HTTPS error.
  CI's Python 3.12 names its DLLs `libcrypto-3.dll` and never collided (2026-09-03).
- The downloads folder is configurable at create time only (`create --downloads-dir`, or the
  GUI wizard's field). `ledger.downloads_dir(instance)` / `Ledger.downloads_dir` is the single
  place the `downloads` name is joined onto an instance path, and `create.Paths.for_instance`
  reads it for every command that opens an existing instance, so nothing past `create` takes
  the flag. Stage JSON stores absolute archive paths, which is why there is no relocation
  command yet -- moving the folder on disk would orphan every recorded path. MO2 needs
  `download_directory` set in `ModOrganizer.ini` when the folder is non-default
  (`profile.mo2_download_directory`), topped up onto an existing ini by
  `profile.ensure_ini_key` the same way the script-extender key is.
- Nexus categories cost two requests per collection revision, not one per mod: the game's
  category list (`nexus.NexusClient.game_info`, v1 `GET /v1/games/<domain>.json`,
  `categories`) and every mod's category from the collection revision's GraphQL
  (`modFiles { file { mod { modCategory { id } } } }`, an id like `"24,1704"` -- Nexus
  category id, then game id). The manifest's `details.category` name is the
  case-insensitive fallback when GraphQL has nothing (some names Vortex records are
  stale). `categories.py` resolves and stores this per layer as
  `c2mo2/<slug>-<rev>.categories.json` (`create.LayerPaths.categories_json`);
  `installer._write_meta_ini` writes `category`/`nexusCategory` into each mod's
  `meta.ini`, and `profile.apply_layer_categories` tops up an already-installed instance.
  MO2 maps the two ids through `categories.dat` and `nexuscatmap.dat` in the instance
  root, numbering categories in ascending Nexus id order the way MO2's own "import Nexus
  categories" does -- but only when *neither* file exists yet; if the instance already has
  a `nexuscatmap.dat` (MO2's or the user's own edits), that numbering is read back and
  used instead, and never overwritten. The fetch is wrapped in a broad catch: this feature
  is purely cosmetic, so any error is one warning and mods are just left uncategorised
  rather than failing the run.
- `--skip-errors` (`create`/`add`/`update`; the GUI's Review page has it as a checkbox)
  carries a run past three failure gates -- download, `inspect`'s 7-Zip listing, and
  `install` -- instead of stopping at the first one; it implies the older, narrower
  `--allow-missing` (file gone from Nexus only; an md5 mismatch still stops that flag
  alone). Each skipped mod becomes a `create.SkippedMod(name, stage, reason)` collected on
  `create.Run.skipped` / `api.CreateResult.skipped`, printed at the end, and written to the
  layer's ledger record (`ledger.py`, read back by `Ledger.layer_skipped`) so `c2mo2
  status` prints `NOT installed (skipped by the last run): N` and the GUI's Manage page
  shows it. A failed `install` entry is kept in `install.json` as `strategy: "failed"`
  (`installer.py:479`) so `c2mo2 install --only <mod> --force` can retry it later;
  `profile.py` filters `strategy == "failed"` entries out of `modlist.txt`. On `update`
  specifically (`update.py`), a mod whose new revision's file fails to download or install
  keeps the previous revision's copy on disk (noted in `install.json`) instead of losing
  the mod entirely; a brand-new mod that fails is simply absent.

- A `source.type == "bundle"` mod is one the curator packed into the collection archive
  itself: the manifest gives it a `fileExpression`, a `logicalFilename` and a `tag`, but
  no `modId`, no `fileId` and no `md5`. `fetch` already unpacks the whole archive, so
  `downloader._download_bundle` needs no network -- it looks under
  `<manifest dir>/bundled/<fileExpression>` (then `logicalFilename`, then a
  case-insensitive stem match). **That entry is usually a folder despite its name ending
  in `.7z`** (`Bundled - khajiit overhaul MR patch.7z (v)/textures/...`), so a folder is
  zipped, contents-rooted, into the downloads folder as `Bundled - <stem>.zip` and a real
  file is copied; downstream stages only ever see an ordinary archive path. The entry
  records the *produced* archive's md5 (there is none in the manifest) and a `.meta` with
  `repository=` empty and no ids. Because a bundle mod has neither md5 nor Nexus ids,
  `profile._entry_index_for_mod` falls back to `tag` after md5 and `(modId, fileId)`, or
  its `modRules` would be dropped; `update._match_old_mods` already tries `tag` first, but
  a bundle mod whose tag Vortex re-issued reads as removed + added, i.e. a reinstall.

- Sign-in (`oauth.py`) is OAuth 2.0 PKCE for a public client: no client secret, a
  loopback callback on `http://127.0.0.1:43119/callback` for the duration of a sign-in,
  and the token pair stored in `keyring` under service `collections2mo2`, chunked across
  several entries (`nexus-oauth`, `nexus-oauth.0`, ...) because Windows Credential
  Manager caps a single secret at 1280 UTF-16 characters. `BearerAuth` attaches the
  access token only to api.nexusmods.com requests, never to CDN download URLs, and
  refreshes it under a lock shared process-wide via `oauth.default_auth()` so parallel
  download workers don't race a refresh. `NEXUS_API_KEY` in `.env` is a developer-only
  override that takes precedence over a stored sign-in. The JWT's claims are decoded
  unverified, for display (`whoami`, the GUI's account chip) only - every real request
  still goes through Nexus and would fail on its own if the token were bad. Every
  request also carries `Application-Name`/`Application-Version` headers alongside the
  existing User-Agent, per Nexus's API Acceptable Use Policy.

## Shared contracts

- `naming.mod_folder_name(mod)` is the single source of truth for MO2 mod folder names; the
  installer and profile writer must agree.
- `downloads.json` -> `inspect.json` -> `install.json` schemas are documented in the module
  docstrings of `downloader.py`, `archive_inspect.py`, `installer.py`.
