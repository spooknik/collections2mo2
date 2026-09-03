"""Compare the game version a collection was built against with the one on disk.

Every collection manifest carries `info.gameVersions` (a list, in practice one entry
like `"1.6.1170.0"`), and the same list is exposed on a revision by GraphQL as
`collectionRevision { gameVersions { reference } }` -- so the check works both before
anything is downloaded (the GUI, from `api.CollectionSummary`) and during a run (from
the fetched manifest).

The installed version comes from the main executable's Windows version resource
(`build.GAME_MAIN_EXE` names the exe per game), read through `version.dll` with
`ctypes`: `GetFileVersionInfoSizeW` -> `GetFileVersionInfoW` -> `VerQueryValueW` on the
root block, which yields a `VS_FIXEDFILEINFO` whose `dwFileVersionMS`/`LS` pack the four
16-bit components. Reading the exe header this way is what Explorer's Details tab shows
and, unlike parsing a `.ini` or asking Steam, it is exactly what SKSE checks.

**This is advisory only.** Nothing here fails a run or blocks the wizard: a mismatch is
a warning, because the fix (downgrading or patching the `Stock Game` copy) can happen
after the instance is built, and because the version list occasionally lags what a
curator actually supports.
"""

from __future__ import annotations

import ctypes
import re
import sys
from pathlib import Path
from typing import Any

from .build import GAME_MAIN_EXE
from .downloader import MO2_GAME_NAMES
from .profile import MO2_GAME_DISPLAY_NAMES

# Number of leading numeric components that have to agree for two versions to be the
# same runtime: Bethesda's fourth component is a build counter that Steam and the
# collection metadata disagree on routinely ("1.6.1170.0" vs "1.6.1170").
_SIGNIFICANT_COMPONENTS = 3

_VS_FFI_SIGNATURE = 0xFEEF04BD


class _VSFixedFileInfo(ctypes.Structure):
    """`VS_FIXEDFILEINFO` from verrsrc.h (plain `DWORD`s, so it is importable anywhere)."""

    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def display_game_name(game_name: str | None) -> str:
    """Any of the three names a caller might hold -> the display name.

    Nexus domain (`skyrimspecialedition`) -> MO2 short name (`SkyrimSE`) -> display name
    (`Skyrim Special Edition`); already-display names and unknown games pass through
    unchanged. The GUI has the domain from the collection summary, `create` has the MO2
    short name, and `build.GAME_MAIN_EXE` is keyed by the display name.
    """
    if not game_name:
        return "the game"
    short = MO2_GAME_NAMES.get(game_name.lower(), game_name)
    return MO2_GAME_DISPLAY_NAMES.get(short, short)


def main_exe_name(game_name: str | None) -> str | None:
    """The main executable for a game, or None if we have no entry for it."""
    return GAME_MAIN_EXE.get(display_game_name(game_name))


def file_version(exe: Path) -> str | None:
    """`major.minor.build.private` from `exe`'s version resource, or None."""
    if sys.platform != "win32":
        return None
    try:
        version_dll = ctypes.WinDLL("version.dll")  # type: ignore[attr-defined]
        path = str(exe)
        size = version_dll.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buffer):
            return None
        block = ctypes.c_void_p()
        block_len = ctypes.c_uint()
        ok = version_dll.VerQueryValueW(
            buffer,
            ctypes.c_wchar_p("\\"),
            ctypes.byref(block),
            ctypes.byref(block_len),
        )
        if not ok or not block.value or block_len.value < ctypes.sizeof(_VSFixedFileInfo):
            return None
        info = ctypes.cast(block, ctypes.POINTER(_VSFixedFileInfo)).contents
        if info.dwSignature != _VS_FFI_SIGNATURE:
            return None
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, AttributeError, ValueError):
        return None


def installed_game_version(game_path: str | Path | None, game_name: str | None) -> str | None:
    """The version of the game installed at `game_path`, or None if it can't be read.

    None on non-Windows, an unknown game, a missing folder or executable, and on any
    failure of the version API -- every caller treats "unknown" as "say nothing loudly".
    """
    if not game_path:
        return None
    exe_name = main_exe_name(game_name)
    if not exe_name:
        return None
    try:
        exe = Path(game_path) / exe_name
        if not exe.is_file():
            return None
    except OSError:
        return None
    return file_version(exe)


def _numeric_parts(version: str) -> list[int]:
    parts: list[int] = []
    for chunk in str(version).strip().split("."):
        match = re.match(r"\d+", chunk)
        if match is None:
            break
        parts.append(int(match.group()))
    return parts


def versions_match(left: str, right: str) -> bool:
    """True when both versions agree on their first three numeric components.

    `1.6.1170.0` == `1.6.1170`; a version with fewer components than that is compared
    on as many as both sides have (`1.6` == `1.6.1170` is *not* a match, since the
    shorter side still has to agree component for component -- it matches `1.6.x` only
    when the other side is also two components).
    """
    left_parts, right_parts = _numeric_parts(left), _numeric_parts(right)
    if not left_parts or not right_parts:
        return False
    depth = min(len(left_parts), len(right_parts), _SIGNIFICANT_COMPONENTS)
    return left_parts[:depth] == right_parts[:depth]


def short_version(version: str) -> str:
    """`1.6.1170.0` -> `1.6.1170`; anything unparseable is returned as-is."""
    parts = _numeric_parts(version)
    if not parts:
        return str(version).strip()
    return ".".join(str(p) for p in parts[:_SIGNIFICANT_COMPONENTS])


def manifest_game_versions(manifest: dict[str, Any] | None) -> list[str]:
    """`info.gameVersions` from a collection manifest, as a list of strings."""
    info = (manifest or {}).get("info") or {}
    versions = info.get("gameVersions") or []
    if isinstance(versions, str):
        versions = [versions]
    return [str(v) for v in versions if str(v).strip()]


def compare_versions(
    collection_versions: list[str],
    installed: str | None,
    *,
    game_name: str | None = None,
    game_path: str | Path | None = None,
) -> tuple[str, str] | None:
    """`(status, message)` for a version check, or None when there is nothing to check.

    `status` is `"match"`, `"mismatch"` or `"unknown"`. Any of the collection's listed
    versions counts as a match. Returns None when the collection lists no versions at
    all -- older manifests and a GraphQL response without the field both land there, and
    silence beats a warning we cannot substantiate.
    """
    wanted = [v for v in (collection_versions or []) if str(v).strip()]
    if not wanted:
        return None
    game = display_game_name(game_name)
    targets = ", ".join(short_version(v) for v in wanted)
    where = str(game_path) if game_path else "your game folder"
    if not installed:
        unknown = (
            f"Could not read the installed {game} version from {where}; this collection "
            f"targets {targets}."
        )
        return "unknown", unknown
    if any(versions_match(v, installed) for v in wanted):
        return "match", f"Game version {short_version(installed)} matches the collection."
    mismatch = (
        f"This collection targets {game} {targets}; the game at {where} is "
        f"{short_version(installed)}. SKSE and DLL mods are built for an exact version "
        "and will not load until the Stock Game copy is the version the collection "
        "expects (see the FAQ on game versions)."
    )
    return "mismatch", mismatch


def check_game_version(
    collection_versions: list[str],
    game_path: str | Path | None,
    game_name: str | None,
) -> tuple[str, str] | None:
    """`compare_versions` against whatever is installed at `game_path`.

    The one call `create.add_layer` and the GUI both make.
    """
    installed = installed_game_version(game_path, game_name)
    return compare_versions(
        collection_versions,
        installed,
        game_name=game_name,
        game_path=game_path,
    )
