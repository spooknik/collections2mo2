"""Shared naming rules so the installer and the profile writer agree on folder names."""

from __future__ import annotations

import hashlib
import re

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Windows MAX_PATH is 260 for the whole path and mod folders sit several levels deep, with
# the mod's own files below them. Some curators use 250+ character names; cap them.
MAX_FOLDER_NAME = 120


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


def separator_name(phase: int) -> str:
    """MO2 separator folder name for a collection install phase (phase 666 = optional mods)."""
    label = "Optional" if phase == 666 else f"Phase {phase}"
    return f"{label}_separator"
