# Development

For architecture notes, module ownership, and non-obvious facts verified against the
live Nexus API, see [`CLAUDE.md`](../CLAUDE.md) - that's the canonical technical
reference this file doesn't try to duplicate.

## Setup

```
uv sync                       # Python >= 3.12
uv run c2wj --help
uv run c2wj-gui
```

`uv sync` installs both the CLI/engine dependencies and the GUI's (PySide6, keyring)
plus the dev group (pytest, pytest-qt, pyinstaller, ruff via pre-commit).

## Tests

```
uv run pytest -q
```

Tests live in `tests/`. A few tests are marked `local` because they need a local tool
(`tools/7za.exe`, bootstrapped by `sevenzip.py` on first real run) that may not be
present in CI - `pytest` runs them when the tool is there and skips them otherwise.

End-to-end verification beyond the unit tests is running the pipeline against a real
collection and spot-checking the output instance in `work/` or an instance directory -
see the development collection below.

## Linting and pre-commit

```
uvx ruff check src        # lint (ruff config in pyproject.toml, line length 100)
uv run pre-commit install # one-time: installs the git pre-commit hook
```

`.pre-commit-config.yaml` runs `ruff check --fix` and `ruff format`, the standard
`pre-commit-hooks` set (`detect-private-key`, `end-of-file-fixer`,
`trailing-whitespace`, `check-added-large-files`), and a local `nexus-secret-check`
hook (`scripts/check_secrets.py`) that refuses to commit a real API key or an `.env`
file.

## Stage-by-stage (what `create` composes)

`create` is `fetch` -> (`survey`) -> `download` -> `inspect` -> `install` -> `profile`
-> `build`, run in-process against one instance directory, resumable, with a ledger
(`c2wj-instance.json`) written at the end. Each stage is also a standalone CLI command
(`docs/cli.md` has every option); useful for debugging one stage in isolation or
inspecting the intermediate JSON.

```
uv run c2wj fetch https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3 --report
uv run c2wj download work/h2uqa3/68/archive/collection.json
uv run c2wj inspect work/h2uqa3/68/downloads/downloads.json
uv run c2wj install work/h2uqa3/68/downloads/inspect.json
uv run c2wj profile work/h2uqa3/68/mo2/install.json --game-path "D:/Games/Skyrim Special Edition"
uv run c2wj build work/h2uqa3/68/mo2 --game-path "D:/Games/Skyrim Special Edition"
```

- `fetch` downloads the collection archive for a revision into
  `work/<slug>/<revision>/archive/` and extracts it. `report` summarises a manifest
  (mods, install modes, sources, phases, mod rules).
- `download` fetches every Nexus-hosted archive into
  `work/<slug>/<revision>/downloads/`, verifies MD5s against the manifest, writes MO2
  `.meta` sidecars, and records `downloads.json`.
- `inspect` opens each archive (via a bundled 7-Zip console binary in `tools/`) and
  records FOMOD presence and layout in `inspect.json`.
- `install` builds `work/<slug>/<revision>/mo2/mods/<Mod Name>/` for every mod and
  records what it did in `install.json`. FOMODs replay the curator's recorded choices;
  fresh-mode FOMODs take installer defaults unless `--choices-overrides` is given.
- `profile` writes `profiles/<name>/modlist.txt`, `plugins.txt`, `loadorder.txt`, the
  profile-local game INIs (seeded from your `My Games` INIs, with the collection's
  `INI Tweaks` merged in) and a portable `ModOrganizer.ini` into the same `mo2/`
  folder.
- `build` downloads a pinned Mod Organizer 2 release and the Root Builder plugin into
  that folder and points `ModOrganizer.ini` at your game. After this,
  `mo2/ModOrganizer.exe` is a working portable instance.
- `survey` is an optional pre-flight: using Nexus's content previews it reports FOMOD
  presence and archive layouts for a collection without downloading it (rate-limited,
  cached, resumable).

Development/verification collection:
[SKSE and Behaviours Essentials (h2uqa3)](https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3),
292 mods, with FOMOD choices, optionals, phases and rules - small enough to iterate on
quickly. `docs/faq.md` and the acceptance notes referenced from `CLAUDE.md` also cover
a full-scale run (Gate to Sovngarde, ~2,000 mods) for the layering/update/tools/
Wabbajack-export paths.

## `work/` layout

```
work/<slug>/<revision>/archive/collection.json         # fetched manifest (+ INI Tweaks/, patches/)
work/<slug>/<revision>/downloads/downloads.json         # download.py output + .meta sidecars
work/<slug>/<revision>/downloads/inspect.json            # archive_inspect.py output
work/<slug>/<revision>/mo2/install.json                  # installer.py output
work/<slug>/<revision>/mo2/mods/                          # the mod folders themselves
work/<slug>/<revision>/mo2/profiles/                      # the rendered MO2 profile
work/<slug>/<revision>/mo2/ModOrganizer.exe               # after `build`
```

A `create`d instance lays these out differently (everything under one `<out>/`, with
per-layer stage JSON under `<out>/c2wj/` and the ledger at `<out>/c2wj-instance.json`)
- see `CLAUDE.md` for that layout.

`work/`, `tools/`, and `.env` are gitignored. **Never print `.env` contents or a real
API key** - the pre-commit hook and `scripts/check_secrets.py` exist specifically to
catch that before it reaches a commit.

## GUI packaging

```
uv run pyinstaller packaging/c2wj-gui.spec
```

Builds a standalone `dist/c2wj-gui/c2wj-gui.exe`. See `packaging/README.md` for the
`C2WJ_DATA_DIR` override (where a frozen build caches 7-Zip and MO2/Root Builder
downloads) and known PyInstaller hook gaps for this project's dependencies
(py7zr's compiled codecs, keyring's backend discovery).
