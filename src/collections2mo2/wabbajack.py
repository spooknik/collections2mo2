"""`c2mo2 wabbajack`: compile a generated MO2 instance into a `.wabbajack` modlist.

The last step of the pipeline. Everything before it produced a portable MO2 instance
(`mods/`, `profiles/`, `downloads/` with `.meta` sidecars, optionally a `Stock Game`
copy and `Tools/`); this stage writes the one file Wabbajack's compiler reads and then
runs `wabbajack-cli.exe compile` over it.

What the compiler actually wants (verified against Wabbajack 4.2.2.1's own binaries and
against real `.compiler_settings` files written by its GUI -- see the notes below, they
are not guesses):

* `wabbajack-cli.exe compile -i <path> -o <dir>`. Those two options are the *whole* verb;
  there is no `--settings`. `-i` is overloaded on the extension: a `.compiler_settings`
  **file** is deserialised as `CompilerSettings` ("Using specified settings file"), and
  anything else is treated as an MO2 root whose settings are reconstructed from
  `ModOrganizer.ini` ("Inferring settings", `CompilerSettingsInferencer.InferFromRootPath`).
  We always pass the settings file, so nothing is inferred and nothing depends on MO2's
  ini layout -- but note that inference is also the only path that *forces*
  `UseGamePaths = true`, so a hand-written settings file has to set it itself or the
  Stock Game copy matches nothing. `-o` only takes effect when it names an **existing
  directory**, and then replaces just the directory of `OutputFile`; a path that is not a
  directory is silently ignored, so `OutputFile` is the value that really decides.
* `<Source>/<ModListName>.compiler_settings` is the GUI's convention (the file lives in
  the source folder root) and is what we write. It is plain JSON, **PascalCase**, and its
  spelling is inconsistent in a way that matters: `ModListName`/`ModListAuthor` but
  `ModlistIsNSFW`/`ModlistVersion` (lower-case `l`). A misspelled key is silently ignored
  by System.Text.Json, so it would compile with the wrong metadata rather than fail.
* `Ignore`, `Include`, `NoMatchInclude` and `AlwaysEnabled` hold paths **relative to
  Source, with backslashes** (`mods\\Some Mod`, `Stock Game\\CreationKit.exe`, `Tools`).
  They are path prefixes, not globs -- so `*.log` has to be expanded to the log files
  that are actually there. `Ignore` drops files; `NoMatchInclude` says "these files have
  no archive to download them from, store them inside the .wabbajack instead", which is
  what makes user mods, tool binaries and generated output compile at all.
* Every archive in `downloads/` needs a `.meta` next to it or the compile aborts with
  "No download metadata found for <archive>, please use MO2 to query info or add a .meta
  file and try again." A meta resolves when `[General]` carries `gameName` + `modID` +
  `fileID` (Nexus) or `directURL` (plain HTTP); those key names come straight out of
  `Wabbajack.Downloaders.Nexus.dll` / `.Http.dll`. `gameName` is MO2's name (`SkyrimSE`),
  which is what `downloader.py` already writes.
* `Stock Game` is not a magic folder name. `UseGamePaths: true` makes the compiler hash
  the *real* game install (`IndexGameFileHashes`), match those hashes against the copy in
  the source folder and emit `GameFileSource` archives plus octodiff patches for the ones
  the collection modified (`PatchStockGameFiles`). So the copy costs hashing time, not
  distribution size, and the folder may be called anything.

`c2mo2 build` also copies the MO2 release archive (and Root Builder's) into the instance's
`downloads/` with a `.meta`, alongside caching them in the repo's `tools/`, so MO2's own
program files (`ModOrganizer.exe`, `dlls/`, `plugins/`, ...) have an archive to be
referenced from and compile as `FromArchive` like any other mod, instead of being
inlined into the `.wabbajack` (~440 MB for 2.5.2). An instance built before that existed
has no such archive in `downloads/`, so those files still inline for it; the checklist
(`check_program_files`) only counts what is genuinely uncovered.

`--dry-run` does all of the above except the compile, and deliberately does not touch the
ledger: it writes only the settings file and `<instance>/wabbajack/`.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import create as create_mod
from . import ledger as ledger_mod
from .reporter import Reporter, get_reporter

# Repo root is two levels above this file, as in build.py; third-party binaries are
# staged in the gitignored tools/ rather than installed system-wide.
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "tools" / "cache"
WJ_DIR = REPO_ROOT / "tools" / "wabbajack"

WJ_LATEST_RELEASE = "https://api.github.com/repos/wabbajack-tools/wabbajack/releases/latest"

SETTINGS_SUFFIX = ".compiler_settings"
OUTPUT_SUBDIR = "wabbajack"

# Instance folders that never belong in a modlist. Wabbajack already ignores `logs`,
# `crashDumps`, `saves`, `webcache` and friends by default; repeating them is harmless
# and documents the intent. `wabbajack/` matters more than it looks: our own output
# folder lives *inside* the compile source, so without this the compiler would index the
# placeholder image, `compile.log` and (on a second run) the previous `.wabbajack` itself,
# none of which any archive can produce.
# "c2wj" is the pre-rename stage folder: an instance built before the project became
# `collections2mo2` still has one if it has not yet been opened (and migrated).
DEFAULT_IGNORE = ["c2mo2", "c2wj", "crashDumps", "logs", "overwrite", OUTPUT_SUBDIR]

# Tool binaries are installed from GitHub/Nexus by `c2mo2 tools`, not from an archive in
# downloads/, so no hash in the list can resolve them: they have to be inlined.
DEFAULT_NOMATCH_INCLUDE = ["Tools"]

INLINE_WARN_BYTES = 500 * 1024 * 1024
LONG_PATH_CHARS = 60

# Nexus domain -> Wabbajack `Game` enum name (the value of the settings' `Game` key).
GAME_NAMES = {
    "morrowind": "Morrowind",
    "oblivion": "Oblivion",
    "oblivionremastered": "OblivionRemastered",
    "fallout3": "Fallout3",
    "newvegas": "FalloutNewVegas",
    "falloutnv": "FalloutNewVegas",
    "skyrim": "Skyrim",
    "skyrimspecialedition": "SkyrimSpecialEdition",
    "skyrimvr": "SkyrimVR",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "enderal": "Enderal",
    "enderalspecialedition": "EnderalSpecialEdition",
    "starfield": "Starfield",
}

# Same table keyed by the MO2 game name, for instances whose ledger predates `domain`.
MO2_GAME_NAMES = {
    "morrowind": "Morrowind",
    "oblivion": "Oblivion",
    "fallout3": "Fallout3",
    "newvegas": "FalloutNewVegas",
    "skyrim": "Skyrim",
    "skyrimse": "SkyrimSpecialEdition",
    "skyrimvr": "SkyrimVR",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "starfield": "Starfield",
}


class WabbajackError(RuntimeError):
    """Something the user has to fix before a compile can happen."""


# -- helpers -------------------------------------------------------------------------


def _rel(path: Path, root: Path) -> str:
    """`path` relative to `root` in Wabbajack's form: backslashes, no leading slash."""
    return str(path.relative_to(root)).replace("/", "\\")


def _win(path: Path | str) -> str:
    """A path as Wabbajack writes them into the settings file."""
    return str(Path(path)).replace("/", "\\")


def safe_name(name: str) -> str:
    """A modlist name reduced to something Windows will accept as a file name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip(" .")
    return cleaned or "Modlist"


def dotnet_version(value: str) -> str:
    """Coerce a version to the `A.B.C[.D]` shape .NET's `Version` can parse.

    Collection revisions are plain integers, so `68` becomes `68.0.0`; anything the
    user passes with a pre-release tag (`1.2.0-rc1`) loses the tag.
    """
    parts = re.findall(r"\d+", str(value or ""))
    if not parts:
        return "0.0.1"
    parts = parts[:4]
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts)


def game_name(domain: str | None, mo2_name: str | None = None) -> str:
    """The Wabbajack `Game` value for a ledger's game section."""
    key = (domain or "").strip().lower()
    if key in GAME_NAMES:
        return GAME_NAMES[key]
    mo2 = (mo2_name or "").strip().lower()
    if mo2 in MO2_GAME_NAMES:
        return MO2_GAME_NAMES[mo2]
    if key:
        # Unknown domain: Wabbajack matches game names case-insensitively, so passing
        # the domain through is a better guess than failing outright.
        return key
    raise WabbajackError(
        "the ledger does not record which game this instance is for; "
        "re-run `c2mo2 build --game-path ...` or edit c2mo2-instance.json"
    )


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


# -- the compiler CLI ----------------------------------------------------------------


def _cli_from_app_dir(app_dir: Path) -> Path | None:
    """`wabbajack-cli.exe` for an unpacked Wabbajack version folder."""
    for candidate in (app_dir / "cli" / "wabbajack-cli.exe", app_dir / "wabbajack-cli.exe"):
        if candidate.is_file():
            return candidate
    return None


def _registry_wabbajack_dir() -> Path | None:
    """The install folder from the `wabbajack:` URL protocol handler.

    Wabbajack's launcher registers `HKCU\\Software\\Classes\\wabbajack\\shell\\open\\command`
    pointing at the `Wabbajack.exe` of the version it last ran, which is the only record
    of *where* the user let it install (it is not always %LOCALAPPDATA%).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\wabbajack\shell\open\command"
        ) as key:
            command, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    m = re.search(r'"([^"]+\.exe)"', str(command)) or re.match(r"(\S+\.exe)", str(command))
    if not m:
        return None
    exe = Path(m.group(1))
    return exe.parent if exe.parent.is_dir() else None


def _saved_settings_dirs() -> list[Path]:
    """Version folders hinted at by `%LOCALAPPDATA%\\Wabbajack\\saved_settings\\*.json`.

    Those files record absolute paths inside the install (`.../<version>/downloaded_mod_lists/x`),
    which is enough to walk back up to a folder holding `cli/wabbajack-cli.exe`.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    saved = Path(local) / "Wabbajack" / "saved_settings"
    if not saved.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(saved.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for hit in re.findall(r'"([A-Za-z]:\\\\[^"]+)"', text):
            candidate = Path(hit.replace("\\\\", "\\")).parent
            for _ in range(4):
                if _cli_from_app_dir(candidate):
                    out.append(candidate)
                    break
                if candidate.parent == candidate:
                    break
                candidate = candidate.parent
    return out


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", name)) or (0,)


def _scan_roots() -> list[Path]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Wabbajack")
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Wabbajack")
    roots.append(WJ_DIR)
    hinted = _registry_wabbajack_dir()
    if hinted is not None:
        roots.extend([hinted, hinted.parent])
    return roots


def find_wabbajack_cli(explicit: str | Path | None = None) -> Path | None:
    """Locate `wabbajack-cli.exe`, or return None.

    Order: an explicit path, `$WABBAJACK_CLI`, `PATH`, the `wabbajack:` protocol handler
    (which names the exact version folder), paths mentioned in Wabbajack's saved settings,
    then a shallow scan of the usual install roots, newest version first.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        if path.is_dir():
            found = _cli_from_app_dir(path)
            if found:
                return found
        raise WabbajackError(f"--wabbajack-cli: no wabbajack-cli.exe at {path}")

    env = os.environ.get("WABBAJACK_CLI")
    if env and Path(env).is_file():
        return Path(env)

    for name in ("wabbajack-cli", "wabbajack-cli.exe"):
        which = shutil.which(name)
        if which:
            return Path(which)

    hinted = _registry_wabbajack_dir()
    if hinted is not None:
        found = _cli_from_app_dir(hinted)
        if found:
            return found

    for app_dir in _saved_settings_dirs():
        found = _cli_from_app_dir(app_dir)
        if found:
            return found

    for root in _scan_roots():
        if not root.is_dir():
            continue
        found = _cli_from_app_dir(root)
        if found:
            return found
        versions = sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: _version_key(d.name),
            reverse=True,
        )
        for version_dir in versions:
            found = _cli_from_app_dir(version_dir)
            if found:
                return found
    return None


def download_wabbajack_cli(reporter: Reporter | None = None) -> Path:
    """Unpack the official Wabbajack release into `tools/wabbajack/<version>/`.

    The `Wabbajack.exe` asset is a GUI launcher and cannot be driven headlessly, but the
    same release also publishes `<version>.zip` -- the unpacked application, CLI
    included -- so a non-interactive bootstrap is possible without running an installer
    or writing outside this repo's gitignored `tools/`.
    """
    rep = get_reporter(reporter)
    resp = requests.get(WJ_LATEST_RELEASE, timeout=60)
    resp.raise_for_status()
    release = resp.json()
    version = str(release.get("tag_name") or "").lstrip("v")
    asset = next(
        (a for a in release.get("assets") or [] if str(a.get("name", "")).endswith(".zip")),
        None,
    )
    if asset is None:
        raise WabbajackError(
            "the latest Wabbajack release publishes no .zip asset; install Wabbajack "
            "yourself and pass --wabbajack-cli"
        )
    target = WJ_DIR / (version or Path(asset["name"]).stem)
    existing = _cli_from_app_dir(target)
    if existing:
        return existing

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = CACHE_DIR / str(asset["name"])
    if not archive.exists():
        rep.log(f"downloading Wabbajack {version} ({human(asset.get('size') or 0)})")
        with requests.get(asset["browser_download_url"], stream=True, timeout=300) as r:
            r.raise_for_status()
            tmp = archive.with_suffix(archive.suffix + ".part")
            with open(tmp, "wb") as fh:
                fh.writelines(r.iter_content(1 << 20))
            os.replace(tmp, archive)

    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    found = _cli_from_app_dir(target)
    if found is None:
        # Some releases wrap everything in a single folder.
        for candidate in sorted(target.rglob("wabbajack-cli.exe")):
            return candidate
        raise WabbajackError(f"{asset['name']} contains no wabbajack-cli.exe")
    return found


# -- the settings file ---------------------------------------------------------------


def _profiles(instance: Path) -> list[str]:
    profiles_dir = instance / "profiles"
    if not profiles_dir.is_dir():
        return []
    return sorted(d.name for d in profiles_dir.iterdir() if d.is_dir())


def _root_logs(instance: Path) -> list[str]:
    """Top-level `*.log` files, expanded because `Ignore` matches prefixes, not globs."""
    return sorted(p.name for p in instance.glob("*.log") if p.is_file())


def _stock_game_dir(instance: Path, led: ledger_mod.Ledger) -> Path | None:
    recorded = (led.data.get("game") or {}).get("stock_game_dir")
    if recorded:
        path = Path(recorded)
        if path.is_dir():
            return path
    default = instance / "Stock Game"
    return default if default.is_dir() else None


def collection_url(led: ledger_mod.Ledger, layer: dict[str, Any] | None) -> str:
    domain = (led.data.get("game") or {}).get("domain")
    slug = (layer or {}).get("slug")
    if not domain or not slug:
        return ""
    return f"https://www.nexusmods.com/games/{domain}/collections/{slug}"


@dataclass
class Defaults:
    """The modlist metadata derived from the ledger, before CLI overrides."""

    name: str
    author: str
    description: str
    version: str
    website: str
    readme: str
    profile: str
    game: str

    @classmethod
    def from_ledger(cls, led: ledger_mod.Ledger) -> Defaults:
        base = led.base_layer() or {}
        name = base.get("name") or base.get("slug") or "Modlist"
        author = base.get("author") or "Unknown"
        url = collection_url(led, base)
        note = "Converted from a Nexus Mods collection with collections2mo2."
        description = f"{name} by {author}. {note}"
        if url:
            description = f"{description} Source collection: {url}"
        profile = base.get("profile") or (_first_profile(led) or name)
        game = led.data.get("game") or {}
        return cls(
            name=name,
            author=author,
            description=description,
            version=dotnet_version(base.get("revision") or 1),
            website=url,
            readme=url,
            profile=profile,
            game=game_name(game.get("domain"), game.get("mo2_name")),
        )


def _first_profile(led: ledger_mod.Ledger) -> str | None:
    found = _profiles(led.instance_dir)
    return found[0] if found else None


def build_settings(
    instance: Path,
    led: ledger_mod.Ledger,
    *,
    name: str | None = None,
    author: str | None = None,
    description: str | None = None,
    version: str | None = None,
    website: str | None = None,
    readme: str | None = None,
    image: str | Path | None = None,
    output: str | Path | None = None,
    no_match_include: list[str] | None = None,
) -> dict[str, Any]:
    """The `CompilerSettings` JSON for this instance.

    Key names and casing follow Wabbajack 4.x exactly -- `ModListName` but
    `ModlistVersion`; see the module docstring.
    """
    instance = Path(instance)
    defaults = Defaults.from_ledger(led)

    list_name = name or defaults.name
    profiles = _profiles(instance)
    profile = (
        defaults.profile
        if defaults.profile in profiles
        else (profiles[0] if profiles else defaults.profile)
    )
    additional = [p for p in profiles if p != profile]

    out_file = (
        Path(output) if output else instance / OUTPUT_SUBDIR / f"{safe_name(list_name)}.wabbajack"
    )

    ignore = list(DEFAULT_IGNORE)
    ignore.extend(_root_logs(instance))

    settings: dict[str, Any] = {
        "ModlistIsNSFW": False,
        "Source": _win(instance),
        "Downloads": _win(instance / "downloads"),
        "Game": defaults.game,
        "OutputFile": _win(out_file),
        "ModListImage": _win(image) if image else "",
        "UseGamePaths": True,
        "UseTextureRecompression": False,
        "OtherGames": [],
        "MaxVerificationTime": "00:01:00",
        "ModListName": list_name,
        "ModListAuthor": author or defaults.author,
        "ModListDescription": description or defaults.description,
        "ModListReadme": readme if readme is not None else defaults.readme,
        "ModListWebsite": website if website is not None else defaults.website,
        "ModListCommunity": "",
        "ModlistVersion": dotnet_version(version or defaults.version),
        "PublishUpdate": False,
        "MachineUrl": "",
        "AutoGenerateReport": False,
        "Profile": profile,
        "AdditionalProfiles": additional,
        "NoMatchInclude": list(no_match_include or DEFAULT_NOMATCH_INCLUDE),
        "Include": [],
        "Ignore": ignore,
        "AlwaysEnabled": [],
        # `Version` is the one the compiler copies onto the modlist; `ModlistVersion`
        # above is [Obsolete] but still round-tripped by the GUI, so both are written
        # with the same value. `Description` is likewise a GUI-only twin of
        # `ModListDescription`.
        "Version": dotnet_version(version or defaults.version),
        "Description": description or defaults.description,
    }
    return settings


def settings_path(instance: Path, settings: dict[str, Any]) -> Path:
    return Path(instance) / f"{safe_name(settings['ModListName'])}{SETTINGS_SUFFIX}"


def write_settings(instance: Path, settings: dict[str, Any]) -> Path:
    """Write `<instance>/<name>.compiler_settings` atomically."""
    path = settings_path(instance, settings)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_placeholder_image(path: Path, width: int = 640, height: int = 360) -> Path:
    """A plain PNG so `ModListImage` is never empty.

    Wabbajack resizes the modlist image while exporting; giving it nothing is an easy way
    to lose a half-hour compile, and a solid-colour PNG costs a few kilobytes.
    """
    rgb = bytes((28, 32, 40))
    raw = b"".join(b"\x00" + rgb * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


# -- the pre-compile checklist -------------------------------------------------------


@dataclass
class ArchiveCheck:
    name: str
    source: str  # "nexus", "direct", "unrecognised", "missing"
    detail: str = ""
    # The archive's parsed `[General]` .meta block, kept around so tool/companion-mod
    # tracing can match on `modID`/`fileID`/`gameName`/`modName`/`directURL` without
    # re-reading every .meta file a second time.
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.source in ("nexus", "direct")


@dataclass
class InlineEntry:
    path: str  # relative, backslash form
    size: int
    reason: str


@dataclass
class TracedEntry:
    """A folder the compiler can reproduce from a `downloads/` archive.

    Reported separately from `InlineEntry`: it costs nothing in the `.wabbajack`
    (the archive is referenced, not stored) so it should neither count towards the
    inline-size warning nor look like something the user needs to fix.
    """

    path: str  # relative, backslash form
    archive: str  # the downloads/ archive name it traced to


@dataclass
class Checklist:
    archives: list[ArchiveCheck] = field(default_factory=list)
    inlined: list[InlineEntry] = field(default_factory=list)
    traced: list[TracedEntry] = field(default_factory=list)
    game_state: list[InlineEntry] = field(default_factory=list)
    stock_game: Path | None = None
    instance_path_len: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def bad_archives(self) -> list[ArchiveCheck]:
        return [a for a in self.archives if not a.ok]

    @property
    def inlined_bytes(self) -> int:
        return sum(e.size for e in self.inlined)


def read_meta(path: Path) -> dict[str, str]:
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.optionxform = str
    try:
        cfg.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not cfg.has_section("General"):
        return {}
    return {k: (v or "").strip() for k, v in cfg["General"].items()}


def classify_meta(meta: dict[str, str]) -> tuple[str, str]:
    """`(source, detail)` for a parsed `[General]` block.

    Mirrors what Wabbajack's downloaders look for: `gameName`/`modID`/`fileID` for the
    Nexus downloader, `directURL` for the HTTP one.
    """
    if not meta:
        return "unrecognised", "no [General] section"
    mod_id = meta.get("modID", "")
    file_id = meta.get("fileID", "")
    game = meta.get("gameName", "")
    if game and mod_id not in ("", "0") and file_id not in ("", "0"):
        return "nexus", f"{game} {mod_id}/{file_id}"
    direct = meta.get("directURL") or meta.get("url") or ""
    if direct.startswith(("http://", "https://")):
        return "direct", direct
    return "unrecognised", "no Nexus modID/fileID and no directURL"


def check_downloads(downloads: Path) -> list[ArchiveCheck]:
    """Every archive in `downloads/` and whether its `.meta` resolves to a source."""
    out: list[ArchiveCheck] = []
    if not downloads.is_dir():
        return out
    for entry in sorted(downloads.iterdir()):
        if not entry.is_file() or entry.suffix.lower() == ".meta":
            continue
        meta_path = entry.with_name(entry.name + ".meta")
        if not meta_path.is_file():
            out.append(ArchiveCheck(entry.name, "missing", "no .meta sidecar"))
            continue
        meta = read_meta(meta_path)
        source, detail = classify_meta(meta)
        out.append(ArchiveCheck(entry.name, source, detail, meta))
    return out


def _archive_by_name(archives: list[ArchiveCheck]) -> dict[str, ArchiveCheck]:
    return {a.name.lower(): a for a in archives}


def _trace_via_installation_file(
    mod_dir: Path, by_name: dict[str, ArchiveCheck]
) -> ArchiveCheck | None:
    """The archive `<mod_dir>/meta.ini`'s `installationFile=` names, if it resolves.

    `installationFile=` is what MO2 (and Wabbajack, reading the same file) uses to tie
    a mod folder back to its archive; `installer.py` and `tools.py`'s companion-mod
    install path both write it, collection-owned or not.
    """
    meta_ini = mod_dir / "meta.ini"
    if not meta_ini.is_file():
        return None
    name = (read_meta(meta_ini).get("installationFile") or "").strip()
    if not name:
        return None
    archive = by_name.get(name.lower())
    return archive if archive and archive.ok else None


def _trace_via_companion_record(
    folder: str, led: ledger_mod.Ledger, by_name: dict[str, ArchiveCheck]
) -> ArchiveCheck | None:
    """The archive a tool's `companion_mods` record points at, for `folder`.

    Fallback for a companion mod installed before it had a `meta.ini` (or one that
    lost it): `tools[id]["companion_mods"]` records the Nexus mod/file id it resolved
    to at install time, which is matched against every archive's `.meta`.
    """
    for owner in led.owners_of(folder):
        if not owner.startswith("tool:"):
            continue
        tool_id = owner.split(":", 1)[1]
        tool_record = (led.data.get("tools") or {}).get(tool_id) or {}
        for companion in tool_record.get("companion_mods") or []:
            if companion.get("folder") != folder:
                continue
            mod_id, file_id = companion.get("mod_id"), companion.get("file_id")
            if not mod_id or not file_id:
                continue
            for archive in by_name.values():
                if not archive.ok:
                    continue
                if str(archive.meta.get("modID") or "") == str(mod_id) and str(
                    archive.meta.get("fileID") or ""
                ) == str(file_id):
                    return archive
    return None


def _trace_mod_archive(
    mod_dir: Path, folder: str, led: ledger_mod.Ledger, by_name: dict[str, ArchiveCheck]
) -> ArchiveCheck | None:
    """The `downloads/` archive that reproduces `mods/<folder>`, if any resolves."""
    return _trace_via_installation_file(mod_dir, by_name) or _trace_via_companion_record(
        folder, led, by_name
    )


def _trace_tool_archive(
    tool_id: str, led: ledger_mod.Ledger, archives: list[ArchiveCheck]
) -> ArchiveCheck | None:
    """The `downloads/` archive that reproduces `Tools/<tool_id>`, if any resolves.

    `tools[id]` records no filename directly, so this matches the archive the same way
    `tools.py`'s own `_write_tool_meta` produced it: first by the tool's display name
    (the `.meta`'s `modName`, which is exactly `tools[id]["name"]`), then by the source
    `tools.py` recorded (Nexus mod id, direct URL, GitHub release-asset pattern), and
    finally a loose substring match as a last resort.
    """
    record = (led.data.get("tools") or {}).get(tool_id) or {}
    name = (record.get("name") or "").strip().lower()
    source = record.get("source") or {}
    kind = source.get("type")
    ok_archives = [a for a in archives if a.ok]

    if name:
        for archive in ok_archives:
            if (archive.meta.get("modName") or "").strip().lower() == name:
                return archive

    if kind == "nexus":
        domain = str(source.get("domain") or "").lower()
        mod_id = str(source.get("mod_id") or "")
        if domain and mod_id:
            for archive in ok_archives:
                if (archive.meta.get("gameName") or "").lower() == domain and str(
                    archive.meta.get("modID") or ""
                ) == mod_id:
                    return archive
    elif kind == "direct":
        url = source.get("url") or ""
        if url:
            for archive in ok_archives:
                if (archive.meta.get("directURL") or "") == url:
                    return archive
    elif kind == "github":
        pattern = None
        asset_pattern = source.get("asset")
        if asset_pattern:
            try:
                pattern = re.compile(asset_pattern)
            except re.error:
                pattern = None
        if pattern:
            for archive in ok_archives:
                if pattern.search(archive.name):
                    return archive

    if name:
        for archive in ok_archives:
            if (
                name in archive.name.lower()
                or name in (archive.meta.get("directURL") or "").lower()
            ):
                return archive
    return None


def _classify_mod_and_tool_folders(
    instance: Path, led: ledger_mod.Ledger, archives: list[ArchiveCheck]
) -> tuple[list[InlineEntry], list[TracedEntry]]:
    """Split `mods/` and `Tools/` into what a `downloads/` archive can reproduce.

    A `mods/` folder the ledger attributes to a collection layer (recorded `md5`) came
    out of an archive already; anything else -- a tool binary, a tool's companion mod,
    a mod the user installed by hand -- is only genuinely inlined once tracing it back
    to a `downloads/` archive fails too. `Tools/<id>` output folders (`DynDOLOD_Output`,
    `TexGen_Output`, ...) are not tools themselves and carry no ledger record, so they
    fall through as ordinary archive-less user mods.
    """
    by_name = _archive_by_name(archives)
    inlined: list[InlineEntry] = []
    traced: list[TracedEntry] = []
    separators = led.separator_folders()
    mods = instance / "mods"
    if mods.is_dir():
        for child in sorted(mods.iterdir()):
            if not child.is_dir() or child.name in separators:
                continue
            record = led.data.get("mods", {}).get(child.name)
            owners = led.owners_of(child.name)
            if record and record.get("md5"):
                continue
            archive = _trace_mod_archive(child, child.name, led, by_name)
            if archive is not None:
                traced.append(TracedEntry(_rel(child, instance), archive.name))
                continue
            reason = "user mod" if owners == [ledger_mod.USER_OWNER] else "no archive recorded"
            inlined.append(InlineEntry(_rel(child, instance), dir_size(child), reason))
    tools = instance / "Tools"
    if tools.is_dir():
        for child in sorted(tools.iterdir()):
            if not child.is_dir():
                continue
            archive = _trace_tool_archive(child.name, led, archives)
            if archive is not None:
                traced.append(TracedEntry(_rel(child, instance), archive.name))
                continue
            inlined.append(InlineEntry(_rel(child, instance), dir_size(child), "tool binaries"))
    return inlined, traced


def check_inlined(instance: Path, led: ledger_mod.Ledger) -> list[InlineEntry]:
    """Folders with no archive behind them, i.e. what ends up inside the `.wabbajack`.

    A `mods/` folder the ledger attributes to a collection layer, a tool binary folder
    a `downloads/` archive traces to, or a tool's companion mod likewise traced, all
    came out of an archive the compiler can reference. Anything else -- mods the user
    installed by hand, tool output, `Tools/` binaries with nothing behind them -- has to
    be stored in the modlist file itself.
    """
    archives = check_downloads(instance / "downloads")
    inlined, _traced = _classify_mod_and_tool_folders(instance, led, archives)
    inlined.extend(check_program_files(instance, led))
    return inlined


def check_traced(instance: Path, led: ledger_mod.Ledger) -> list[TracedEntry]:
    """`mods/`/`Tools/` folders a `downloads/` archive can reproduce -- not inlined."""
    archives = check_downloads(instance / "downloads")
    _inlined, traced = _classify_mod_and_tool_folders(instance, led, archives)
    return traced


def _read_build_meta(instance: Path) -> dict[str, Any]:
    path = instance / "c2mo2-build.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _release_archives_covered(instance: Path, build_meta: dict[str, Any]) -> set[str]:
    """Top-level instance entries a `downloads/` archive now accounts for.

    `c2mo2 build` (since the fix below) copies the MO2 release archive into
    `downloads/` with a `.meta` and records the top-level names it wrote
    (`ModOrganizer.exe`, `dlls`, `plugins`, ...) as `mo2_top_level` in
    `c2mo2-build.json`. When that archive is genuinely present and its `.meta`
    resolves, those names will compile as `FromArchive` instead of being inlined, so
    `check_program_files` should not count them. An instance built before this existed
    (no `c2mo2-build.json`, or one with no `mo2_top_level`) has no such archive and
    falls back to the old behaviour: everything not otherwise accounted for is inlined.
    """
    version = build_meta.get("mo2_version")
    top_level = build_meta.get("mo2_top_level") or []
    if not version or not top_level:
        return set()
    archive_name = f"Mod.Organizer-{version}.7z"
    archive_path = instance / "downloads" / archive_name
    meta_path = archive_path.with_name(archive_path.name + ".meta")
    if not archive_path.is_file() or not meta_path.is_file():
        return set()
    source, _ = classify_meta(read_meta(meta_path))
    if source not in ("nexus", "direct"):
        return set()
    return {name.lower() for name in top_level}


def check_program_files(instance: Path, led: ledger_mod.Ledger) -> list[InlineEntry]:
    """MO2's own program files that no archive in `downloads/` can produce.

    A hand-built Wabbajack modlist keeps the Mod Organizer release archive in `downloads/`
    with a `.meta`, so the compiler emits `ModOrganizer.exe`, `dlls\\`, `plugins\\` and the
    rest as `FromArchive` directives (verified by reading the `modlist` manifest out of a
    real `.wabbajack`). `c2mo2 build` now does the same (see `_ensure_release_download` in
    build.py) and records which top-level names came from that archive, so those are no
    longer counted here; only files with genuinely nothing behind them -- and instances
    built before this existed -- are inlined.
    """
    stock = _stock_game_dir(instance, led)
    covered = _release_archives_covered(instance, _read_build_meta(instance))
    skip = {name.lower() for name in DEFAULT_IGNORE}
    skip.update({"mods", "downloads", "profiles", "tools", "webcache", "saves", "__temp__"})
    if stock is not None:
        skip.add(stock.name.lower())
    out: list[InlineEntry] = []
    for child in sorted(instance.iterdir()):
        # The settings file names itself, and top-level logs are already in `Ignore`.
        if child.name.lower() in skip or child.name.endswith((SETTINGS_SUFFIX, ".log")):
            continue
        if child.name.lower() in covered:
            continue
        size = dir_size(child) if child.is_dir() else child.stat().st_size
        out.append(InlineEntry(_rel(child, instance), size, "MO2 program file"))
    return out


def check_game_state(instance: Path, led: ledger_mod.Ledger) -> list[InlineEntry]:
    """Tool state left inside the Stock Game copy, which no game hash can account for.

    Most of the Stock Game folder resolves against the installed game (`UseGamePaths`),
    but collections run patchers in there and those leave bookkeeping behind -- the
    Runtime Swapper keeps `.skyrim-runtime-swapper\\` with its downgrade backups, a
    `transaction.lock` and `.complete` marker files. A single unmatched file is fatal:

        [WARN]  Stock Game\\.skyrim-runtime-swapper\\backups\\1.7.104\\.complete\\1.6.1170-9.complete
                - No Match in Stack
        [FATAL] Exiting due to no way to compile these files

    A dot-prefixed entry in a Bethesda game folder is never game content, always tool
    state, so those go into `NoMatchInclude` as a safety net. Their size is reported
    separately from the inlined total: the backups inside them are byte-identical copies
    of game files and resolve through `DirectMatch` long before `NoMatchInclude` is
    reached, so only the genuine leftovers are actually stored.
    """
    stock = _stock_game_dir(instance, led)
    if stock is None:
        return []
    out: list[InlineEntry] = []
    for child in sorted(stock.iterdir()):
        if not child.name.startswith("."):
            continue
        size = dir_size(child) if child.is_dir() else child.stat().st_size
        out.append(InlineEntry(_rel(child, instance), size, "game-folder tool state"))
    return out


def no_match_include(checklist: Checklist) -> list[str]:
    """`NoMatchInclude` for a checklist: `Tools` plus everything else with no archive.

    Anything the compiler cannot resolve to a download has to be listed here or it falls
    through the whole compilation stack. `Tools` covers every tool folder as a prefix, so
    those are not repeated.
    """
    out = list(DEFAULT_NOMATCH_INCLUDE)
    covered = {p.lower() for p in DEFAULT_NOMATCH_INCLUDE}
    prefixes = tuple(p + "\\" for p in covered)
    for entry in [*checklist.inlined, *checklist.game_state]:
        lowered = entry.path.lower()
        if lowered in covered or lowered.startswith(prefixes):
            continue
        out.append(entry.path)
    return out


def precompile_checklist(instance: Path, led: ledger_mod.Ledger) -> Checklist:
    instance = Path(instance)
    archives = check_downloads(instance / "downloads")
    inlined, traced = _classify_mod_and_tool_folders(instance, led, archives)
    inlined.extend(check_program_files(instance, led))
    checklist = Checklist(
        archives=archives,
        inlined=inlined,
        traced=traced,
        game_state=check_game_state(instance, led),
        stock_game=_stock_game_dir(instance, led),
        instance_path_len=len(str(instance)),
    )
    bad = checklist.bad_archives
    if bad:
        checklist.warnings.append(
            f"{len(bad)} archive(s) in downloads/ have no usable .meta; the compiler will "
            "abort on the first one ('No download metadata found for ...')"
        )
    if checklist.inlined_bytes > INLINE_WARN_BYTES:
        checklist.warnings.append(
            f"{human(checklist.inlined_bytes)} of files have no download behind them and "
            f"will be stored inside the .wabbajack (over the {human(INLINE_WARN_BYTES)} "
            "rule of thumb); consider trimming Tools/ or user mods"
        )
    program = sum(e.size for e in checklist.inlined if e.reason == "MO2 program file")
    if program:
        if _release_archives_covered(instance, _read_build_meta(instance)):
            # The MO2 release archive is in downloads/ and covers ModOrganizer.exe,
            # dlls/, plugins/ etc. already; what is left is instance-specific state
            # (ModOrganizer.ini, the ledger, portable.txt, ...) that could never come
            # from an upstream archive.
            checklist.warnings.append(
                f"{human(program)} of instance-specific top-level files (ModOrganizer.ini, "
                "the c2mo2 ledger, portable.txt, ...) will be inlined -- there is no archive "
                "these could come from, which is expected and usually small"
            )
        else:
            checklist.warnings.append(
                f"{human(program)} of Mod Organizer's own program files will be inlined: no "
                "Mod.Organizer-<version>.7z (with a resolving .meta) was found in downloads/, "
                "so the compiler has nothing to match them against. This instance was likely "
                "built before `c2mo2 build` started copying that archive into downloads/ for "
                "this reason; re-run `c2mo2 build` to add it"
            )
    if checklist.stock_game is None:
        checklist.warnings.append(
            "no Stock Game folder: the compile will reference the user's own game install "
            "directly, so any file the collection patched in place cannot be reproduced"
        )
    if checklist.instance_path_len > LONG_PATH_CHARS:
        checklist.warnings.append(
            f"instance path is {checklist.instance_path_len} characters; Wabbajack nests "
            "temporary folders under it and Windows still caps full paths at 260"
        )
    return checklist


def report_checklist(checklist: Checklist, rep: Reporter) -> None:
    ok = [a for a in checklist.archives if a.ok]
    nexus = sum(1 for a in ok if a.source == "nexus")
    direct = sum(1 for a in ok if a.source == "direct")
    rep.log(
        f"downloads: {len(checklist.archives)} archive(s); "
        f"{nexus} Nexus, {direct} direct URL, {len(checklist.bad_archives)} unusable"
    )
    for bad in checklist.bad_archives[:20]:
        rep.log(f"  ! {bad.name}: {bad.detail}")
    if len(checklist.bad_archives) > 20:
        rep.log(f"  ! ... and {len(checklist.bad_archives) - 20} more")

    if checklist.traced:
        rep.log(
            f"traced to a downloads/ archive (not inlined): {len(checklist.traced)} "
            "folder(s) -- tool binaries and tool-owned companion mods the compiler "
            "can reference instead of storing"
        )

    rep.log(
        f"inlined into the modlist: {len(checklist.inlined)} folder(s), {human(checklist.inlined_bytes)}"
    )
    for entry in sorted(checklist.inlined, key=lambda e: -e.size)[:20]:
        rep.log(f"  - {entry.path}  {human(entry.size)}  ({entry.reason})")
    if len(checklist.inlined) > 20:
        rep.log(f"  - ... and {len(checklist.inlined) - 20} more")

    if checklist.stock_game is not None:
        rep.log(f"Stock Game: {checklist.stock_game}")
    for entry in checklist.game_state:
        rep.log(
            f"  ~ {entry.path}  {human(entry.size)}  ({entry.reason}; most of it should "
            "match the installed game, only leftovers are stored)"
        )
    for warning in checklist.warnings:
        rep.warn(warning)


# -- the compile ---------------------------------------------------------------------


def run_compile(
    cli: Path,
    settings_file: Path,
    output_file: Path,
    *,
    log_file: Path | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`wabbajack-cli.exe compile -i <settings> -o <output dir>`, streamed line by line.

    `-o` wants a directory, not a file (it swaps the directory of the settings'
    `OutputFile` and keeps the file name), so the output folder is what gets passed.

    **Nothing this function writes may live under the compile source while the compiler
    runs.** `VFS.Context.AddRoots` hashes every file under `Source` *before* the `Ignore`
    list is consulted, opening each one with `File.Open(..., FileAccess.Read,
    FileShare.Read)` -- which fails against a handle that has the file open for writing:

        Unhandled exception: System.IO.IOException: The process cannot access the file
        'E:\\...\\wabbajack\\compile.log' because it is being used by another process.
           at Wabbajack.VFS.FileHashCache.FileHashCachedAsync(...)
           at Wabbajack.VFS.Context.AddRoots(...)

    So the transcript is streamed to a scratch directory and moved to `log_file` at the
    end, and the compiler runs with its working directory there too (it writes its own
    `logs/wabbajack-cli.current.log` relative to the CWD and holds that open as well).
    """
    rep = get_reporter(reporter)
    out_dir = output_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(cli), "compile", "-i", str(settings_file), "-o", str(out_dir)]
    rep.log("running: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

    work = Path(tempfile.mkdtemp(prefix="c2mo2-wabbajack-"))
    scratch_log = work / "compile.log"
    try:
        with contextlib.ExitStack() as stack:
            handle = stack.enter_context(open(scratch_log, "w", encoding="utf-8", newline="\n"))
            proc = stack.enter_context(
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(work),
                )
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                handle.write(line + "\n")
                handle.flush()
                rep.log(line)
            code = proc.wait()
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(scratch_log), str(log_file))
        return code
    finally:
        shutil.rmtree(work, ignore_errors=True)


def record_in_ledger(
    instance: Path,
    settings_file: Path,
    output_file: Path,
    version: str,
    *,
    compiled_at: str | None,
) -> Path:
    """Merge a `wabbajack` section into `c2mo2-instance.json` (atomic write).

    Written through `ledger.Ledger.save`, which replaces the file via a temp file in the
    same directory, so a half-written ledger is never observable.
    """
    led = ledger_mod.load(instance)
    led.data["wabbajack"] = {
        "settings": str(settings_file),
        "output": str(output_file),
        "version": version,
        "compiled_at": compiled_at,
    }
    return led.save()


# -- Command -------------------------------------------------------------------------


def cmd_wabbajack(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    rep.stage("wabbajack")

    instance = Path(args.instance).expanduser().resolve()
    if not instance.is_dir():
        rep.warn(f"{instance} is not a directory")
        return 1
    create_mod.migrate_legacy_instance(create_mod.Paths(instance), rep)
    if not (instance / ledger_mod.LEDGER_NAME).exists():
        rep.warn(
            f"{instance / ledger_mod.LEDGER_NAME} not found: this does not look like a "
            "c2mo2 instance (run `c2mo2 create` first)"
        )
        return 1

    led = ledger_mod.load(instance)
    try:
        checklist = precompile_checklist(instance, led)
        no_match = no_match_include(checklist)

        settings = build_settings(
            instance,
            led,
            name=args.name,
            author=args.author,
            description=args.description,
            version=args.version,
            website=args.website,
            readme=args.readme,
            image=args.image,
            output=args.output,
            no_match_include=no_match,
        )
    except WabbajackError as e:
        rep.warn(str(e))
        return 1

    out_dir = instance / OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if not settings["ModListImage"]:
        image = write_placeholder_image(out_dir / f"{safe_name(settings['ModListName'])}.png")
        settings["ModListImage"] = _win(image)
        rep.log(f"no --image given; wrote a placeholder at {image}")

    written = write_settings(instance, settings)
    rep.log(f"wrote {written}")
    rep.log(
        f"modlist: {settings['ModListName']} {settings['ModlistVersion']} "
        f"by {settings['ModListAuthor']} [{settings['Game']}]"
    )
    rep.log(
        f"profile: {settings['Profile']}  additional: {settings['AdditionalProfiles'] or 'none'}"
    )
    report_checklist(checklist, rep)

    output_file = Path(settings["OutputFile"])
    if args.dry_run:
        cli = None
        try:
            cli = find_wabbajack_cli(args.wabbajack_cli)
        except WabbajackError as e:
            rep.warn(str(e))
        rep.log(f"wabbajack-cli: {cli}" if cli else "wabbajack-cli: not found (would download)")
        rep.done("wabbajack", f"dry run; settings at {written}, would write {output_file}")
        return 0

    try:
        cli = find_wabbajack_cli(args.wabbajack_cli)
        if cli is None:
            rep.log("no Wabbajack install found; fetching the official release")
            cli = download_wabbajack_cli(rep)
    except (WabbajackError, requests.RequestException) as e:
        rep.warn(str(e))
        rep.warn("pass --wabbajack-cli <path to wabbajack-cli.exe> to use an existing install")
        return 1
    rep.log(f"wabbajack-cli: {cli}")

    if checklist.bad_archives:
        rep.warn(
            "compiling anyway, but the archives listed above have no usable .meta and the "
            "compiler is expected to stop on them"
        )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    code = run_compile(
        cli,
        written,
        output_file,
        log_file=out_dir / "compile.log",
        reporter=rep,
    )
    elapsed = (datetime.now(UTC) - started).total_seconds()
    if code != 0 or not output_file.exists():
        rep.warn(f"wabbajack-cli compile exited {code} after {elapsed / 60:.1f} min")
        rep.warn(f"full compiler output: {out_dir / 'compile.log'}")
        return code or 1

    size = output_file.stat().st_size
    record_in_ledger(
        instance,
        written,
        output_file,
        settings["ModlistVersion"],
        compiled_at=datetime.now(UTC).isoformat(),
    )
    rep.done(
        "wabbajack",
        f"{output_file} ({human(size)}) in {elapsed / 60:.1f} min",
    )
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "wabbajack",
        help="compile an instance into a .wabbajack modlist with wabbajack-cli",
    )
    p.add_argument("--instance", required=True, help="the MO2 instance directory to compile")
    p.add_argument("--name", default=None, help="modlist name (default: the base collection's)")
    p.add_argument(
        "--version",
        default=None,
        help="modlist version, .NET style (default: <collection revision>.0.0)",
    )
    p.add_argument("--author", default=None, help="modlist author (default: the curator)")
    p.add_argument("--description", default=None, help="modlist description")
    p.add_argument("--website", default=None, help="modlist website (default: the collection URL)")
    p.add_argument(
        "--readme", default=None, help="modlist readme URL (default: the collection URL)"
    )
    p.add_argument(
        "--image", default=None, help="modlist image; a placeholder is generated if omitted"
    )
    p.add_argument(
        "--output",
        default=None,
        help="output file (default: <instance>/wabbajack/<name>.wabbajack)",
    )
    p.add_argument(
        "--wabbajack-cli",
        default=None,
        help="path to wabbajack-cli.exe (default: auto-detect, else download the release)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="write the settings file and print the checklist, but do not compile",
    )
    p.set_defaults(func=cmd_wabbajack)
