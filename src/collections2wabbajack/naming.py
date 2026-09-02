"""Shared naming rules so the installer and the profile writer agree on folder names."""

from __future__ import annotations

import hashlib
import re

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Windows MAX_PATH is 260 for the whole path. The instance path, the mod folder and the
# mod's own (often deeply nested) files all share that budget, so keep folder names short.
# Some curators use 250+ character names; cap them. 80 leaves ~180 for the rest.
MAX_FOLDER_NAME = 80

# MO2 recognises a mod folder whose name ends in this as a separator row.
SEP_SUFFIX = "_separator"


def sanitize_folder_name(name: str) -> str:
    """Make a collection mod name safe as an MO2 mod folder name.

    Mirrors MO2's fixDirectoryName: collapse whitespace, drop characters illegal on
    Windows, strip trailing dots/spaces, avoid DOS device names. Never returns "".
    Names longer than MAX_FOLDER_NAME are truncated and suffixed with a short hash of
    the full name so they stay unique and deterministic.
    """
    cleaned = " ".join((name or "").split())
    cleaned = _ILLEGAL.sub("", cleaned).rstrip(". ")
    if not cleaned or cleaned.upper() in _RESERVED:
        cleaned = f"{cleaned}_mod" if cleaned else "unnamed_mod"
    if len(cleaned) > MAX_FOLDER_NAME:
        digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:6]
        cleaned = f"{cleaned[: MAX_FOLDER_NAME - 8].rstrip('. ')} ~{digest}"
    return cleaned


def mod_folder_name(mod: dict) -> str:
    """MO2 mod folder / modlist.txt name for a collection manifest mod entry."""
    return sanitize_folder_name(mod.get("name") or "")


def with_suffix(base: str, suffix: str) -> str:
    """`base` with ` ~<suffix>` appended, staying within `MAX_FOLDER_NAME`."""
    tail = f" ~{suffix}"
    return f"{base[: MAX_FOLDER_NAME - len(tail)].rstrip('. ')}{tail}"


def assign_folder_names(
    mods: list[dict],
    *,
    taken: dict[str, str] | None = None,
    suffix: str = "",
) -> dict[str, str]:
    """Map every manifest mod's `source.tag` to a unique MO2 folder name.

    Curators can list the same mod name twice with different files (GTS does). The
    first occurrence keeps the plain name; later ones get a ` ~<tag>` suffix so they
    do not overwrite each other. Deterministic for a given manifest.

    `taken` makes the assignment *layer-aware*: it maps folder names already present
    in the instance (from an earlier collection layer) to the md5 of the archive that
    produced them. A new mod whose name collides with one of those is left on the
    same folder when the md5 matches -- it is literally the same file, so the two
    layers share one folder -- and gets a ` ~<suffix>` folder of its own when it does
    not. `suffix` is normally the new layer's slug.
    """
    external = {k.lower(): (v or "") for k, v in (taken or {}).items()}
    used: set[str] = set()
    result: dict[str, str] = {}
    for mod in mods:
        source = mod.get("source") or {}
        tag = source.get("tag") or mod.get("name") or ""
        md5 = source.get("md5") or ""
        base = mod_folder_name(mod)
        folder = base
        if suffix and folder.lower() in external and external[folder.lower()] != md5:
            folder = with_suffix(base, suffix)
        if folder.lower() in used:
            folder = with_suffix(base, tag[:6])
        used.add(folder.lower())
        result[tag] = folder
    return result


def separator_name(phase: int) -> str:
    """MO2 separator folder name for a collection install phase (phase 666 = optional mods)."""
    label = "Optional" if phase == 666 else f"Phase {phase}"
    return f"{label}_separator"


def layer_separator_name(collection_name: str, slug: str = "") -> str:
    """MO2 separator folder name for a whole add-on collection layer."""
    label = sanitize_folder_name(collection_name or slug or "Collection")
    if label.lower().endswith(SEP_SUFFIX):
        return label
    return sanitize_folder_name(f"{label}{SEP_SUFFIX}")

