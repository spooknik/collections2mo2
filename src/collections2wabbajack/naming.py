"""Shared naming rules so the installer and the profile writer agree on folder names."""

from __future__ import annotations

import re

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_folder_name(name: str) -> str:
    """Make a collection mod name safe as an MO2 mod folder name.

    Mirrors MO2's fixDirectoryName: collapse whitespace, drop characters illegal on
    Windows, strip trailing dots/spaces, avoid DOS device names. Never returns "".
    """
    cleaned = " ".join((name or "").split())
    cleaned = _ILLEGAL.sub("", cleaned).rstrip(". ")
    if not cleaned or cleaned.upper() in _RESERVED:
        cleaned = f"{cleaned}_mod" if cleaned else "unnamed_mod"
    return cleaned


def mod_folder_name(mod: dict) -> str:
    """MO2 mod folder / modlist.txt name for a collection manifest mod entry."""
    return sanitize_folder_name(mod.get("name") or "")


def separator_name(phase: int) -> str:
    """MO2 separator folder name for a collection install phase (phase 666 = optional mods)."""
    label = "Optional" if phase == 666 else f"Phase {phase}"
    return f"{label}_separator"
