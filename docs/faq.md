# FAQ

## My resolution/vsync/window setting was ignored

Some collections ship **SSE Display Tweaks**, which overrides `SkyrimPrefs.ini`'s
display settings entirely - so a plain INI edit gets silently overwritten by the
collection's own copy every time.

Set your choice in the wizard's Display page, or from the CLI:

```
c2wj profile-instance --instance <instance-dir> --resolution 2560x1440 --window borderless --vsync off
```

Whenever you choose anything other than "keep", `c2wj` also writes a small,
top-priority override mod ("c2wj Display Settings") on top of SSE Display Tweaks, so
your choice always wins regardless of what the collection itself ships. The choice is
remembered in the instance's ledger and re-applied (refreshing that override mod)
every time you `add`, `remove`, or `update` a layer - you don't have to set it again
each time. `c2wj profile-instance --forget-display` clears the memory and removes the
generated mod, reverting to whatever the collection's own settings are.

## MO2 shows `SkyrimRuntimeSwapper.exe` when I launch through SKSE on Gate to Sovngarde

This is by design, not a bug. Some large collections need a specific game executable
version and ship a Runtime Swapper that downgrades the game at launch and reverts it
on exit. Seeing it run is expected.

## The first Mod Organizer start after a build takes a minute or more

MO2 indexes every mod folder on its first start. For a 2,000-mod collection that can
take well over a minute; the GUI's Progress page shows a note about this after
`create` finishes, and the "Launch Mod Organizer" button gives MO2 that time before
declaring itself done.

## xEdit/DynDOLOD only see vanilla plugins, not the collection's

xEdit-family tools (xEdit, TexGen, DynDOLOD) read the game's Data folder from the
Windows registry by default, which points at your real Steam install rather than
MO2's virtual filesystem - so with `--stock-game`, they'd only see the vanilla game,
not your mods. Current catalogue entries pass `-D:"<Stock Game>\Data"` to fix this.
If you built your instance before this was added, run:

```
c2wj tools refresh --mo2-dir <instance-dir>
```

This only rewrites the `[customExecutables]` arguments for tools already installed -
no re-download, no re-extraction, and every other executable entry is left alone.

## "Path too long" / file system errors during install or build

Windows has a 260-character path limit, and a 2,000-mod instance with deeply nested
archive layouts can bump into it. Mod folder names are already capped and
deduplicated, but the instance's own path still counts against the budget. Install to
something short, like `D:\GTS`, not a long nested path under `Documents`.

## The Runtime Swapper says "A required patch is missing"

Your `Stock Game` copy isn't the exact game version the collection was built against
(for example, Gate to Sovngarde V117 expects Skyrim SE 1.7.104). Re-copy the game from
a Steam install at the right version, or use Steam's downgrade/branch tools to get
there before building.

## I'm not a Nexus Premium member - what happens?

The collection manifest itself only needs a logged-in personal API key (any account).
**Automatic mod downloads require Nexus Premium** - the Nexus API only issues direct
download links to Premium accounts, so `create`/`download` will fail at the download
step without it. Manual-download support (falling back to a browser download when a
link isn't available) is not implemented yet.

## Can I add my own mods to a converted instance?

Yes. Drop them into `mods/` (or install them through MO2 itself) - `c2wj` only tracks
ownership of the mods it installed, so anything else is treated as yours and keeps its
position in `modlist.txt` across `add`, `remove`, and `update`.

## A FOMOD mod installed with settings I didn't choose

For mods where Vortex recorded no FOMOD answers ("fresh install" mode - the curator
never touched that installer, or it was added after the collection's answers were
recorded), `c2wj` takes the installer's own defaults. This is listed explicitly in
the run's log/output so you can review and reinstall with `--choices-overrides` if you
want something different.

## Where is my Nexus API key stored?

- **GUI**: Windows Credential Manager, via `keyring`. Never written to disk in plain
  text, never sent anywhere but Nexus Mods itself.
- **CLI**: your local `.env` file (`NEXUS_API_KEY=...`), which is gitignored and never
  printed by any command.

## What happens if a collection pins a file that's since been deleted from Nexus?

The download 404s and the run stops by default, since a missing pinned file usually
means something about the mod list changed underneath the curator. Pass
`--allow-missing` to carry on without it - those mods are listed in the summary
instead. An MD5 mismatch (the file exists but isn't the one the collection expects)
still stops the run either way; that's a different, more serious kind of problem.

## Does `c2wj` support Vortex "replicate" mode patches?

Not yet. `hashes`-mode mods (Vortex's "Replicate" - an exact file list, optionally
with binary `patches` against an existing file) are supported for the exact-file-list
part; the `patches` field itself isn't implemented and produces a warning rather than
being applied. This is uncommon in practice - most collections use FOMOD or fresh
installs.

## Can I convert a collection for a game other than Skyrim Special Edition?

The game-domain mapping covers Skyrim, Skyrim VR, Fallout 4, Fallout New Vegas,
Fallout 3, and Oblivion in addition to Skyrim SE, but only Skyrim SE has been verified
end to end. Other games may work but haven't been tested.
