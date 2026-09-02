"""Turn a generated portable MO2 instance into a runnable one.

Consumes the `mo2_dir` written by `profile` (which already has `mods/`, `profiles/`,
`downloads/`, `overwrite/` and `ModOrganizer.ini`) and lays the Mod Organizer 2 program
files plus the Root Builder plugin on top of it, so `<mo2_dir>/ModOrganizer.exe` starts
as a portable instance.

Downloads (the MO2 release 7z and the Root Builder zip) are cached in
`<repo>/tools/cache/` -- see sevenzip.py for why `tools/` (gitignored) is where this
project stages third-party binaries instead of installing them system-wide.

Archive layouts, verified by hand against MO2 2.5.2 / Root Builder 5.1.1 before writing
the extraction logic below:

- The MO2 release 7z has `ModOrganizer.exe`, `dlls/`, `plugins/`, `stylesheets/`, etc.
  directly at its root -- no wrapping folder, and no `ModOrganizer.ini` / `profiles` /
  `mods` / `downloads` / `overwrite` entries anywhere in it. `_flatten_single_root`
  below is defensive for a future release that *does* wrap everything in one folder;
  it is a no-op against 2.5.2.
- The Root Builder zip has a single top-level `rootbuilder/` folder holding everything
  (`base/`, `common/`, `plugin/`, `__init__.py`, ...) -- no loose top-level files. So
  extracting it straight into `<mo2_dir>/plugins/` already produces the
  `<mo2_dir>/plugins/rootbuilder/...` layout the plugin loader expects.

ModOrganizer.ini is rewritten line-by-line, not via configparser: MO2's own ini format
wraps string values containing backslashes in Qt's `@ByteArray(...)` (with each
backslash doubled) and uses keys like `1\binary` inside `[customExecutables]`, both of
which configparser mangles.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

import requests

from .sevenzip import extract

# Repo root is two levels above this file: src/collections2wabbajack/build.py
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "tools" / "cache"

MO2_URL_TEMPLATE = (
    "https://github.com/ModOrganizer2/modorganizer/releases/download/"
    "v{ver}/Mod.Organizer-{ver}.7z"
)
ROOTBUILDER_URL_TEMPLATE = (
    "https://github.com/Kezyma/ModOrganizer-Plugins/releases/download/"
    "rootbuilder/rootbuilder.{ver}.zip"
)

# Instance-owned top-level paths that must survive an MO2 extraction into an
# already-populated mo2_dir. The MO2 release archive does not currently contain any
# of these (verified above), so this is a defensive backstop, not the normal path.
_PROTECTED_NAMES = {"mods", "profiles", "downloads", "overwrite", "ModOrganizer.ini"}

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_CHUNK_SIZE = 1 << 20  # 1 MiB


# -- Downloading --------------------------------------------------------------------


def _cache_path(url: str) -> Path:
    return CACHE_DIR / url.rsplit("/", 1)[-1]


def _download_cached(url: str, dest: Path) -> Path:
    """Download `url` into `dest` unless it is already cached there."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"using cached {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url}")
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(tmp_dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(
                        f"\r  {dest.name}: {downloaded / 1e6:.1f}/{total / 1e6:.1f} MB "
                        f"({pct:.0f}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r  {dest.name}: {downloaded / 1e6:.1f} MB", end="", flush=True)
    print()
    tmp_dest.replace(dest)
    return dest


# -- Extraction -----------------------------------------------------------------------


def _flatten_single_root(temp_dir: Path) -> Path:
    """If the extracted archive nests everything under one folder, return that folder."""
    entries = list(temp_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return temp_dir


def _merge_into(src_root: Path, dest_root: Path) -> tuple[list[str], list[str]]:
    """Move every top-level entry of `src_root` into `dest_root`.

    Skips (leaves untouched) any entry named in `_PROTECTED_NAMES` that already
    exists in `dest_root` -- those are instance files owned by `profile`, not MO2
    program files.
    """
    moved: list[str] = []
    skipped: list[str] = []
    for entry in sorted(src_root.iterdir()):
        target = dest_root / entry.name
        if entry.name in _PROTECTED_NAMES and target.exists():
            skipped.append(entry.name)
            continue
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(entry), str(target))
        moved.append(entry.name)
    return moved, skipped


def _extract_mo2(archive: Path, mo2_dir: Path) -> tuple[list[str], list[str]]:
    with tempfile.TemporaryDirectory(prefix="c2wj-mo2-") as tmp:
        tmp_path = Path(tmp)
        extract(archive, tmp_path)
        root = _flatten_single_root(tmp_path)
        mo2_dir.mkdir(parents=True, exist_ok=True)
        return _merge_into(root, mo2_dir)


def _extract_rootbuilder(archive: Path, mo2_dir: Path) -> list[str]:
    plugins_dir = mo2_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="c2wj-rootbuilder-") as tmp:
        tmp_path = Path(tmp)
        extract(archive, tmp_path)
        candidates = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.lower() == "rootbuilder"]
        src = candidates[0] if candidates else tmp_path
        dest = plugins_dir / "rootbuilder"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
        return sorted(p.name for p in dest.iterdir())


# -- ModOrganizer.ini rewrite ---------------------------------------------------------


def _to_windows_bytearray(path: str) -> str:
    """MO2's own gamePath format: backslash-separated, wrapped in @ByteArray(...), each
    backslash doubled (this is how Qt's INI writer escapes '\\' in a string value)."""
    win = path.replace("/", "\\")
    doubled = win.replace("\\", "\\\\")
    return f"@ByteArray({doubled})"


def _to_forward_slashes(path: str) -> str:
    return path.replace("\\", "/")


def _rewrite_ini(ini_path: Path, game_path: str) -> list[str]:
    """Line-based rewrite of gamePath/game_edition and customExecutables paths.

    Everything else in the file is preserved verbatim, line for line.
    """
    changes: list[str] = []
    lines = ini_path.read_text(encoding="utf-8").splitlines()
    game_path_win = _to_windows_bytearray(game_path)
    game_path_fwd = _to_forward_slashes(game_path)

    out: list[str] = []
    section = ""
    seen_game_path = False
    seen_edition = False

    def close_general() -> None:
        nonlocal seen_game_path, seen_edition
        if section != "General":
            return
        if not seen_game_path:
            out.append(f"gamePath={game_path_win}")
            changes.append(f"added gamePath={game_path_win}")
            seen_game_path = True
        if not seen_edition:
            out.append("game_edition=Steam")
            changes.append("added game_edition=Steam")
            seen_edition = True

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            close_general()
            section = stripped[1:-1]
            out.append(line)
            continue

        if section == "General":
            if stripped.startswith("gamePath="):
                new_line = f"gamePath={game_path_win}"
                if new_line != line:
                    changes.append(f"gamePath: {line!r} -> {new_line!r}")
                out.append(new_line)
                seen_game_path = True
                continue
            if stripped.startswith("game_edition="):
                seen_edition = True
                out.append(line)
                continue

        if section == "customExecutables":
            m = re.match(r"^(\d+)\\binary=(.*)$", line)
            if m:
                idx, value = m.group(1), m.group(2)
                if value and not _DRIVE_RE.match(value):
                    new_value = f"{game_path_fwd}/{value}"
                    out.append(f"{idx}\\binary={new_value}")
                    changes.append(f"{idx}\\binary: {value!r} -> {new_value!r}")
                    continue
                out.append(line)
                continue
            m = re.match(r"^(\d+)\\workingDirectory=(.*)$", line)
            if m:
                idx, value = m.group(1), m.group(2)
                if value == "":
                    out.append(f"{idx}\\workingDirectory={game_path_fwd}")
                    changes.append(f"{idx}\\workingDirectory: '' -> {game_path_fwd!r}")
                    continue
                out.append(line)
                continue

        out.append(line)

    close_general()  # file ended while still inside [General]

    ini_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return changes


# -- Command ----------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    mo2_dir = Path(args.mo2_dir)
    mo2_dir.mkdir(parents=True, exist_ok=True)

    mo2_url = MO2_URL_TEMPLATE.format(ver=args.mo2_version)
    rb_url = ROOTBUILDER_URL_TEMPLATE.format(ver=args.rootbuilder_version)

    mo2_archive = _download_cached(mo2_url, _cache_path(mo2_url))
    rb_archive = _download_cached(rb_url, _cache_path(rb_url))

    mo2_exe = mo2_dir / "ModOrganizer.exe"
    if args.force or not mo2_exe.exists():
        moved, skipped = _extract_mo2(mo2_archive, mo2_dir)
        msg = f"extracted MO2 {args.mo2_version}: {len(moved)} top-level entries written to {mo2_dir}"
        if skipped:
            msg += f"; preserved existing instance files: {', '.join(skipped)}"
        print(msg)
    else:
        print(f"MO2 {args.mo2_version} already present at {mo2_exe} (use --force to re-extract)")

    rb_dir = mo2_dir / "plugins" / "rootbuilder"
    if args.force or not rb_dir.exists():
        members = _extract_rootbuilder(rb_archive, mo2_dir)
        print(f"extracted Root Builder {args.rootbuilder_version} into {rb_dir} ({len(members)} item(s))")
    else:
        print(f"Root Builder already present at {rb_dir} (use --force to re-extract)")

    if args.game_path:
        ini_path = mo2_dir / "ModOrganizer.ini"
        if not ini_path.exists():
            print(f"error: {ini_path} not found; run `c2wj profile` first", file=sys.stderr)
            return 1
        changes = _rewrite_ini(ini_path, args.game_path)
        print(f"rewrote {ini_path}: {len(changes)} change(s)")
        for c in changes:
            print(f"  - {c}")

    launch = mo2_dir / "ModOrganizer.exe"
    print(f"\nDone. Launch with:\n  {launch}")
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "build",
        help="lay down MO2 program files and Root Builder into a generated portable instance",
    )
    p.add_argument(
        "mo2_dir",
        help="portable MO2 instance directory (already has mods/, profiles/, ModOrganizer.ini)",
    )
    p.add_argument(
        "--game-path",
        default=None,
        help="path to the game install; rewrites gamePath (and related paths) in ModOrganizer.ini",
    )
    p.add_argument(
        "--mo2-version", default="2.5.2", help="Mod Organizer 2 release to install (default: 2.5.2)"
    )
    p.add_argument(
        "--rootbuilder-version",
        default="5.1.1",
        help="Root Builder plugin release to install (default: 5.1.1)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="re-extract MO2/Root Builder even if already present",
    )
    p.set_defaults(func=cmd_build)
