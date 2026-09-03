# FAQ

## Windows says the download is a virus

Windows Defender (or SmartScreen) may quarantine `c2mo2-gui.exe` from the releases page,
usually under a generic, machine-learning name such as `Trojan:Win32/Wacatac` or
`Program:Win32/Wacapew.C!ml`. It is a false positive, and a well-known one:

- **It's a PyInstaller build.** Every PyInstaller program starts with the same small
  "bootloader" stub, and because malware authors use the same free tool, antivirus vendors
  have learned to distrust that stub on sight. Since 0.1.1 the release workflow compiles its
  own bootloader instead of shipping the shared prebuilt one, which clears most of these
  detections, and the exe carries a proper version resource (publisher, product, version),
  which Defender's heuristics also weigh.
- **It isn't code-signed** and has no download history with Microsoft, so it gets no
  "reputation" credit. Signing is being looked into.
- **What it does looks suspicious** to a behaviour scanner: it downloads 7-Zip and Mod
  Organizer 2, unpacks hundreds of archives and writes thousands of files.

What to do:

1. **Verify the file.** GitHub shows a SHA-256 digest next to every release asset; compare it
   with `Get-FileHash .\c2mo2-gui-<tag>-windows-x64.zip` in PowerShell. A matching digest
   means the zip is exactly what the release workflow built from the tagged source (the
   build log is public under the repository's Actions tab).
2. **Restore it.** Windows Security -> Virus & threat protection -> Protection history ->
   pick the item -> Actions -> Restore (or Allow). Then add the folder you unzipped it into
   under Virus & threat protection settings -> Exclusions, or Defender will take it again on
   the next scan.
3. **Or skip the exe.** `uv sync` then `uv run c2mo2-gui` runs the same code from source
   with no packaged binary involved (see the Quick start in the README).
4. **Report it.** Microsoft clears specific false positives at
   <https://www.microsoft.com/en-us/wdsi/filesubmission>; more reports get a hash cleared
   faster. Opening an issue with the detection name helps too, so the maintainer can submit
   the same build.

## My resolution/vsync/window setting was ignored

Some collections ship **SSE Display Tweaks**, which overrides `SkyrimPrefs.ini`'s
display settings entirely - so a plain INI edit gets silently overwritten by the
collection's own copy every time.

Set your choice in the wizard's Display page, or from the CLI:

```
c2mo2 profile-instance --instance <instance-dir> --resolution 2560x1440 --window borderless --vsync off
```

Whenever you choose anything other than "keep", `c2mo2` also writes a small,
top-priority override mod ("c2mo2 Display Settings") on top of SSE Display Tweaks, so
your choice always wins regardless of what the collection itself ships. The choice is
remembered in the instance's ledger and re-applied (refreshing that override mod)
every time you `add`, `remove`, or `update` a layer - you don't have to set it again
each time. `c2mo2 profile-instance --forget-display` clears the memory and removes the
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
c2mo2 tools refresh --mo2-dir <instance-dir>
```

This only rewrites the `[customExecutables]` arguments for tools already installed -
no re-download, no re-extraction, and every other executable entry is left alone.

## "Path too long" / file system errors during install or build

Windows has a 260-character path limit, and a 2,000-mod instance with deeply nested
archive layouts can bump into it. Mod folder names are already capped and
deduplicated, but the instance's own path still counts against the budget. Install to
something short, like `D:\GTS`, not a long nested path under `Documents`.

## Where should I put the instance?

On a drive with room to spare, in a short folder of its own - `D:\GTS`, `C:\Modding\GTS`.
The wizard and `c2mo2 create` warn (they never refuse) when the folder you picked is one
of these:

- **Long paths.** Windows caps a full path at 260 characters and mod files nest deeply
  inside the instance; 40 characters or fewer leaves room for the deepest file.
- **Program Files / Program Files (x86).** Windows protects those folders, and MO2, its
  plugins and the mods themselves write there constantly - UAC and file virtualisation
  break that.
- **The Windows folder.** Windows' own directory: permission-restricted and serviced by
  Windows Update. Never put an instance there.
- **Documents, the Desktop, or OneDrive.** Backup, sync and antivirus tools scan every
  one of the tens of thousands of files a collection installs, OneDrive can sync or lock
  a file mid-install, and both start the path a long way from the drive root.
- **Inside a Steam library (`steamapps`).** Steam verifies and updates the files under
  its library and can overwrite or delete the instance.
- **Inside the game folder.** With "Copy the game into the instance" (`--stock-game`)
  that would copy the game into itself. Keep the instance in its own folder.

## How does this tool handle different versions of Skyrim?

It never changes your game version. It does check it, and warns if it does not match what
the collection expects - a warning only, never a reason to stop. What it does:

- **It works on a copy.** With "Copy the game into the instance" (`--stock-game`), the
  game folder your `--game-path` points at is copied into `<instance>\Stock Game`, and MO2
  is pointed at the copy. Every DLL patch, SKSE build and downgrade then touches the copy;
  Steam's install stays exactly as Steam left it, and a later Steam update does not touch
  the instance.
- **The collection says which version it wants.** Every collection manifest records the
  game version the curator built against (for example `1.6.1170.0`), and the curator
  usually states it on the collection's Nexus page. Skyrim SE and AE are the same Steam
  app and the same Nexus game; "AE" means the 1.6.x runtime, with or without the
  Creation Club content.
- **Match it before you build.** SKSE and every DLL plugin are compiled for an exact
  executable version; on the wrong one SKSE refuses to start or plugins fail to load.
  If your Steam install is not the version the collection names, either use Steam's
  depot/branch tools to get there first, or build anyway and run a downgrader patcher
  against `<instance>\Stock Game` afterwards. Because it is a copy, that is safe.
- **Some collections handle it themselves.** Gate to Sovngarde and others ship a
  Runtime Swapper that swaps the executable to the required version at launch and
  reverts it on exit (see the entry above). Those still expect the `Stock Game` copy to
  be the version they say.
- **You get told before you build.** The wizard's "Install location and game" page and
  `c2mo2 create` read your game executable's version and compare it with the one the
  collection records. A match is a one-line confirmation; a difference is a warning that
  names both versions. Neither ever blocks the run - the whole point of the `Stock Game`
  copy is that you can fix the version afterwards.

`c2mo2 update` follows *collection* revisions, not game updates. If Steam updates the
game under you, the instance keeps running on its own copy.

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

Yes. Drop them into `mods/` (or install them through MO2 itself) - `c2mo2` only tracks
ownership of the mods it installed, so anything else is treated as yours and keeps its
position in `modlist.txt` across `add`, `remove`, and `update`.

## A FOMOD mod installed with settings I didn't choose

For mods where Vortex recorded no FOMOD answers ("fresh install" mode - the curator
never touched that installer, or it was added after the collection's answers were
recorded), `c2mo2` takes the installer's own defaults. This is listed explicitly in
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

## Does `c2mo2` support Vortex "replicate" mode patches?

Not yet. `hashes`-mode mods (Vortex's "Replicate" - an exact file list, optionally
with binary `patches` against an existing file) are supported for the exact-file-list
part; the `patches` field itself isn't implemented and produces a warning rather than
being applied. This is uncommon in practice - most collections use FOMOD or fresh
installs.

## Can I convert a collection for a game other than Skyrim Special Edition?

The game-domain mapping covers Skyrim, Skyrim VR, Fallout 4, Fallout New Vegas,
Fallout 3, and Oblivion in addition to Skyrim SE, but only Skyrim SE has been verified
end to end. Other games may work but haven't been tested.

## I removed the base collection from an instance. How do I get it back?

The folder still has MO2, the Stock Game copy, your downloads and any installed tools -
only the collection is gone. Either open the folder in the GUI's **Manage** tab and use
**Set up a collection here**, which runs the normal create wizard pinned to that folder,
or run `c2mo2 create <collection url> --out <folder>` against it. Either way the existing
`downloads/` are reused, so nothing is re-downloaded.

Note that the GUI no longer lets you remove the base collection in the first place:
removing it is CLI-only (`c2mo2 remove <slug> --force`).

## I have an instance built by the old `c2wj` name. Does it still work?

Yes. The first time any `c2mo2` command or the GUI opens it, the `c2wj-instance.json`
ledger, the `c2wj/` folder, `c2wj-build.json`, and the generated display-settings mod
are renamed to their `c2mo2` names automatically, and a stored API key or
already-downloaded tools under the old per-user folder are still found.
