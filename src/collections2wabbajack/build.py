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
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
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

# ModOrganizer.ini gameName= (the game plugin's display name, as written by
# profile.py's MO2_GAME_DISPLAY_NAMES) -> the game's main executable. Used to detect
# an already-completed stock game copy so `build --stock-game` can skip re-copying.
# Games not listed here just skip that presence check (the copy always runs, but
# robocopy itself is a fast no-op when nothing changed).
GAME_MAIN_EXE: dict[str, str] = {
    "Skyrim Special Edition": "SkyrimSE.exe",
    "Skyrim": "TESV.exe",
    "Skyrim VR": "SkyrimVR.exe",
    "Fallout 4": "Fallout4.exe",
    "New Vegas": "FalloutNV.exe",
    "Starfield": "Starfield.exe",
}

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


# -- Stock game copy -------------------------------------------------------------------


def _read_ini_game_name(ini_path: Path) -> str | None:
    """Read `gameName=` from the `[General]` section of ModOrganizer.ini, if present."""
    section = ""
    for line in ini_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "General" and stripped.startswith("gameName="):
            return stripped[len("gameName=") :]
    return None


def _run_robocopy(src: Path, dst: Path) -> list[str]:
    """Copy `src` into `dst` with robocopy; returns its `Files :` / `Bytes :` summary rows.

    robocopy's own exit-code convention: 0-7 are success (some combination of files
    copied/skipped/mismatched/extra), >=8 means a real failure.
    """
    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        "robocopy",
        str(src),
        str(dst),
        "/E",
        "/COPY:DAT",
        "/DCOPY:T",
        "/MT:8",
        "/R:2",
        "/W:2",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NP",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode >= 8:
        raise RuntimeError(
            f"robocopy failed (exit code {result.returncode}) copying {src} -> {dst}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(("Files :", "Bytes :"))
    ]


def _ensure_stock_game(args: argparse.Namespace, mo2_dir: Path, ini_path: Path) -> Path:
    """Copy `--game-path` into the stock game dir (unless already done) and return it."""
    stock_dir = Path(args.stock_game_dir) if args.stock_game_dir else mo2_dir / "Stock Game"
    game_name = _read_ini_game_name(ini_path)
    main_exe = GAME_MAIN_EXE.get(game_name) if game_name else None

    if not args.force_stock and stock_dir.exists() and main_exe and (stock_dir / main_exe).exists():
        print(
            f"stock game copy already present at {stock_dir} ({main_exe} found); "
            "skipping copy (use --force-stock to re-copy)"
        )
        return stock_dir

    print(f"copying game files: {args.game_path} -> {stock_dir} (robocopy) ...")
    start = time.perf_counter()
    summary = _run_robocopy(Path(args.game_path), stock_dir)
    elapsed = time.perf_counter() - start
    for line in summary:
        print(f"  {line}")
    print(f"stock game copy finished in {elapsed:.1f}s")

    print(
        "note: Steam must be running to launch the game; mods and patchers (e.g. a "
        f"collection's Runtime Swapper) will modify the copy at {stock_dir}, not your "
        "Steam install."
    )
    return stock_dir


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
    if args.stock_game and not args.game_path:
        print("error: --stock-game requires --game-path", file=sys.stderr)
        return 1

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

    stock_dir: Path | None = None
    effective_game_path = args.game_path
    if args.game_path or args.stock_game:
        ini_path = mo2_dir / "ModOrganizer.ini"
        if not ini_path.exists():
            print(f"error: {ini_path} not found; run `c2wj profile` first", file=sys.stderr)
            return 1

        if args.stock_game:
            stock_dir = _ensure_stock_game(args, mo2_dir, ini_path)
            effective_game_path = str(stock_dir)

        changes = _rewrite_ini(ini_path, effective_game_path)
        print(f"rewrote {ini_path}: {len(changes)} change(s)")
        for c in changes:
            print(f"  - {c}")

    # MO2 treats a folder holding portable.txt as a portable instance without asking.
    marker = mo2_dir / "portable.txt"
    if not marker.exists():
        marker.write_text("", encoding="utf-8")
        print(f"created {marker}")

    resolved = str(mo2_dir.resolve())
    if len(resolved) > 60:
        print(
            f"warning: instance path is {len(resolved)} characters long ({resolved}). "
            "Windows limits full paths to 260 characters and mod files nest deeply; "
            "prefer a short location such as D:\\GTS for real installs."
        )
    if stock_dir is not None:
        resolved_stock = str(stock_dir.resolve())
        if len(resolved_stock) > 60:
            print(
                f"warning: stock game path is {len(resolved_stock)} characters long "
                f"({resolved_stock}). Game files nest deeply; prefer a short instance "
                "location such as D:\\GTS for real installs."
            )

    build_meta_path = mo2_dir / "c2wj-build.json"
    meta: dict = {}
    if build_meta_path.exists():
        try:
            meta = json.loads(build_meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta.update(
        {
            "mo2_version": args.mo2_version,
            "rootbuilder_version": args.rootbuilder_version,
            "game_path_source": str(Path(args.game_path).resolve()) if args.game_path else None,
            "stock_game_dir": str(stock_dir.resolve()) if stock_dir is not None else None,
            "built_at": datetime.now(UTC).isoformat(),
        }
    )
    build_meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {build_meta_path}")

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
    p.add_argument(
        "--stock-game",
        action="store_true",
        default=False,
        help=(
            "copy --game-path into the instance (Wabbajack's 'Stock Game' convention) and "
            "point MO2 at the copy, so collections that patch game files in place (e.g. a "
            "Runtime Swapper) never touch the real Steam install; requires --game-path"
        ),
    )
    p.add_argument(
        "--stock-game-dir",
        default=None,
        help="where to copy the game to (default: <mo2_dir>/Stock Game)",
    )
    p.add_argument(
        "--force-stock",
        action="store_true",
        default=False,
        help="re-copy the stock game even if the destination already looks populated",
    )
    p.set_defaults(func=cmd_build)
