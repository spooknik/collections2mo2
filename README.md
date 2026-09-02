# collections2wabbajack

Turn a Nexus Mods collection into a Mod Organizer 2 instance, and optionally a Wabbajack modlist,
without Vortex.

## Plan

1. **Manifest** (done): fetch a collection revision's archive, extract `collection.json`, report
   install modes, sources, patches, rules.
2. **Downloads + inspection** (done): download every Nexus archive with MD5 verification
   and MO2 `.meta` sidecars; open each archive to find FOMOD installers and layout.
3. **Install + profile** (done, unverified in a real MO2 yet): build `mods/` from the archives (replicate file lists,
   FOMOD choice replay, MO2-style layout normalisation, Root Builder placement for game-root
   mods), then order mods by phase and `modRules` and write the MO2 profile and instance files.
4. **Wabbajack**: compile the resulting MO2 instance with `wabbajack-cli`.

Development collection: [SKSE and Behaviours Essentials (h2uqa3)](https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3),
292 mods, about 1 GB, with FOMOD choices, optionals, phases and rules.

## Quick start

```
uv sync
copy .env.example .env      # then paste your personal Nexus API key into .env
uv run c2wj create https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3 ^
    --out "E:\c2wj-test\h2uqa3" --game-path "E:\Games\Skyrim Special Edition" --stock-game
uv run c2wj tools install xedit bethini-pie --mo2-dir "E:\c2wj-test\h2uqa3"
```

`create` runs every stage below into one self-contained MO2 instance (resumable; re-running
skips finished stages), copies the game into `Stock Game` so patchers never touch your Steam
install, and records ownership of every mod in `c2wj-instance.json`. `tools` installs optional
modding tools under `Tools\` and registers them as MO2 executables (`c2wj tools list`).

## Stage-by-stage

```
uv sync
copy .env.example .env      # then paste your personal Nexus API key into .env
uv run c2wj fetch https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3 --report
uv run c2wj download work/h2uqa3/68/archive/collection.json
uv run c2wj inspect work/h2uqa3/68/downloads/downloads.json
```

- `fetch` downloads the collection archive for the latest published revision (or `--revision N`)
  into `work/<slug>/<revision>/archive/` and extracts it. `report` summarises a manifest.
- `download` fetches every Nexus-hosted archive into `work/<slug>/<revision>/downloads/`, verifies
  MD5s against the manifest, writes MO2 `.meta` sidecars, and records `downloads.json`.
- `inspect` opens each archive (via a bundled 7-Zip console binary in `tools/`) and records FOMOD
  presence and layout in `inspect.json`.
- `install` builds `work/<slug>/<revision>/mo2/mods/<Mod Name>/` for every mod and records what it
  did in `install.json`. FOMODs replay the curator's recorded choices; fresh-mode FOMODs take
  installer defaults unless you pass `--choices-overrides`.
- `profile` writes `profiles/<name>/modlist.txt`, `plugins.txt`, `loadorder.txt`, the
  profile-local game INIs (seeded from your `My Games` INIs, with the collection's `INI Tweaks`
  merged in) and a portable `ModOrganizer.ini` into the same `mo2/` folder.
- `build` downloads a pinned Mod Organizer 2 release and the Root Builder plugin into that folder
  and points `ModOrganizer.ini` at your game. After this, `mo2/ModOrganizer.exe` is a working
  portable instance.
- `survey` is an optional pre-flight: using Nexus's content previews it reports FOMOD presence and
  archive layouts for a collection without downloading it (rate-limited, cached, resumable).

```
uv run c2wj install work/h2uqa3/68/downloads/inspect.json
uv run c2wj profile work/h2uqa3/68/mo2/install.json --game-path "E:/Games/Skyrim Special Edition"
uv run c2wj build work/h2uqa3/68/mo2 --game-path "E:/Games/Skyrim Special Edition"
```

Verified so far: the h2uqa3 instance opens in MO2 2.5.2 with all 292 mods valid, all plugins
present, phase separators, and Root Builder content in place.

Downloading mod files requires Nexus Premium (the API only issues direct links to Premium
accounts). The manifest itself only needs a logged-in API key.
