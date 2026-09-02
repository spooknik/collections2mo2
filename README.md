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

## Layering collections

An instance can hold several collections. `add` installs an add-on collection (one that
assumes the base list is already present) on top of an existing instance as a new layer;
`remove` takes it back off.

```
uv run c2wj add https://www.nexusmods.com/games/skyrimspecialedition/collections/xk05aw ^
    --instance "E:\c2wj-test\h2uqa3"
uv run c2wj remove xk05aw --instance "E:\c2wj-test\h2uqa3"
```

Every mod a layer installs is owned by `collection:<slug>@<rev>` in `c2wj-instance.json`.
A mod both collections pin to the *same* file (SKSE, Address Library, ...) keeps one
folder with two owners and is not reinstalled; one that clashes by name only gets its own
`<name> ~<slug>` folder. The profile is then rendered from all layers at once: the base
collection's block with its phase separators, then each add-on behind a
`<Collection name>_separator`, with `modRules` resolved across the layers so an add-on can
order itself against a base mod. Mods you installed by hand keep their place in
`modlist.txt`, and so do MO2's own DLC / Creation Club rows.

If a collection pins a Nexus file its author has since deleted, the download 404s forever;
`--allow-missing` carries on without those mods and lists them (an md5 mismatch still stops
the run).

`remove` deletes only the folders that layer alone owns, drops it from the owners of the
shared ones, reverts exactly the INI keys it set (the ledger records the value each key
had before it), and re-renders the profile. Archives stay in `downloads/` unless you pass
`--purge-downloads`. Removing the base layer is refused without `--force`.

## Updating a collection

Collections move: the curator publishes a new revision every few weeks, a handful of mods
different from the last. `update` works out that difference and applies only it.

```
uv run c2wj update --instance "E:\c2wj-test\h2uqa3" --dry-run
uv run c2wj update --instance "E:\c2wj-test\h2uqa3" --yes
```

Without `--to` it goes to the latest published revision; `--to 67` pins a specific one
(older is allowed and is applied as a delta like any other). `--layer <slug>` picks which
collection to move when the instance has more than one. `--dry-run` prints the plan --
the curator's changelog for the target revision, a diff of the collection's install
instructions, and every mod it would add, change or remove -- and stops; without
`--yes` the same plan is printed and confirmed on stdin first.

What "only the delta" means, per mod:

- **unchanged** (same file, same FOMOD answers, same name): nothing is downloaded or
  extracted. The folder simply changes hands to `collection:<slug>@<newrev>`.
- **changed**: a new file, new `choices` or a new file list is re-downloaded and
  reinstalled over the same folder; a mod the curator merely renamed has its folder
  renamed; a mod that only moved `phase` or became optional costs nothing but a
  re-render.
- **added**: downloaded and installed.
- **removed**: the folder is deleted when this layer is its only owner. If it has more
  files than the install recorded, or files newer than the install, it is kept and
  reported instead -- that is how a mod you edited by hand survives an update.

Mods are matched between the two revisions by `(modId, fileId)`, then `md5`, then
`modId`: Vortex re-issues `source.tag` on every revision, so the tags never line up.

Afterwards the profile is re-rendered from every layer, so your own mods keep their
place in `modlist.txt`, MO2's DLC/Creation Club rows stay put, and the INI keys the old
revision set are reverted before the new one's are applied. The ledger records the new
revision and remembers the old one in `previous_revisions`; the old manifest is kept
under `c2wj/collections/<slug>/<oldrev>/` for diffing (pass `--purge-old` to delete it).

`status` is the read-only view of an instance -- it never writes anything:

```
uv run c2wj status --instance "E:\c2wj-test\h2uqa3"
```

It lists each layer with its installed revision against the newest published one (one
GraphQL call per layer, `--offline` skips them), how many `mods/` folders belong to a
collection, a tool or to you, the tools installed, and whether the rendered profile still
matches the ledger.

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
