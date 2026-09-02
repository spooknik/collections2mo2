"""Normalise a plain (non-FOMOD) mod archive into an MO2 mod folder layout.

This mirrors what MO2's "simple installer" does when you drop an archive on it:
work out which directory inside the archive is the game's ``Data`` folder and
make *that* the mod folder root, so ``meshes/``, ``textures/``, plugins and BSAs
end up where the virtual file system expects them.

Two Skyrim-specific wrinkles on top:

* **Root-folder mods** (``details.type == "dinput"`` in the collection manifest,
  e.g. SKSE, SSE Engine Fixes part 2, ``d3dcompiler_47.dll``) ship files that
  belong next to ``SkyrimSE.exe``, not in ``Data``. MO2 handles those through
  Root Builder, which takes everything under a ``Root/`` folder inside the mod.
* Some archives carry a ``vortex_override_instructions.json`` written by the mod
  author telling Vortex exactly what to copy where. When present it wins.

:func:`plan_layout` is a pure function over a file listing so it can be unit
tested without touching an archive.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Directories that only ever appear *inside* a game Data folder. If a directory
# holds one of these it is the Data folder, not a wrapper around it.
DATA_DIRS = {
    "meshes",
    "textures",
    "scripts",
    "interface",
    "skse",
    "sound",
    "music",
    "strings",
    "seq",
    "shadersfx",
    "video",
    "grass",
    "lodsettings",
    "dialogueviews",
    "mcm",
    "nemesis_engine",
    "calientetools",
    "tools",
    "source",
    "docs",
    "platform",
    "skypatcher",
    "pandora_engine",
    "netscriptframework",
    "mapmarkers",
    "shaders",  # Community Shaders features live in Data/Shaders/
}
# Extensions that only make sense inside Data.
DATA_EXTS = {".esp", ".esm", ".esl", ".bsa", ".ini", ".bsl", ".modgroups"}
# The subset that is decisive on its own -- an .ini can live anywhere.
PLUGIN_EXTS = {".esp", ".esm", ".esl", ".bsa"}
# A loose file with one of these extensions sitting directly in a level marks
# that level as Data, even with no recognised subdirectory (rule C). For dinput
# (root-folder) mods only plugins/BSAs count: their loose .dll/.exe/.ini/.json
# files (SKSE loader, Runtime Swapper manifest, preloader configs) belong under
# Root/, so they must not make the level look like Data and stop the descent.
DATA_ISH_LOOSE_EXTS_NON_ROOT = {".esp", ".esm", ".esl", ".bsa", ".ini", ".json", ".pex", ".dll"}
DATA_ISH_LOOSE_EXTS_ROOT_MOD = PLUGIN_EXTS
# Loose files at the top of an archive that are documentation, never content.
README_EXTS = {".txt", ".md", ".pdf", ".url", ".jpg", ".jpeg", ".png"}
# Junk that installers/zip tools leave behind. Never content, never counts
# towards "this folder has loose files", never blocks unwrapping.
JUNK_DIR_NAMES = {"__macosx"}
JUNK_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

OVERRIDE_JSON = "vortex_override_instructions.json"
MAX_DESCEND = 3


@dataclass
class LayoutPlan:
    """A plan for one archive: how it was classified and what to copy where."""

    strategy: str
    files: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().strip("/")


def _is_junk(path: str) -> bool:
    """True for Mac/Windows zip cruft that never counts as real content (rule A)."""
    parts = path.split("/")
    if any(part.lower() in JUNK_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1]
    if name.lower() in JUNK_FILE_NAMES:
        return True
    return name.startswith("._")


def _paths(entries: Iterable[Any]) -> list[str]:
    """Accept plain relative paths or sevenzip ArchiveEntry-ish objects."""
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            path, is_dir = entry, False
        else:
            path = getattr(entry, "path", "") or ""
            is_dir = bool(getattr(entry, "is_dir", False))
        path = _norm(path)
        if path and not is_dir and not _is_junk(path):
            out.append(path)
    return out


def _under(path: str, node: str) -> bool:
    if not node:
        return True
    return path.lower().startswith(node.lower() + "/")


def _relative(path: str, node: str) -> str:
    return path[len(node) + 1 :] if node else path


def _children(files: list[str], node: str) -> tuple[dict[str, str], list[str]]:
    """Direct children of `node`: {lowercased dir name: dir name} and file names."""
    dirs: dict[str, str] = {}
    plain: list[str] = []
    for path in files:
        if not _under(path, node):
            continue
        rest = _relative(path, node)
        head, sep, _ = rest.partition("/")
        if sep:
            dirs.setdefault(head.lower(), head)
        else:
            plain.append(head)
    return dirs, plain


def _looks_like_data(files: list[str], node: str, mod_type: str = "") -> bool:
    dirs, plain = _children(files, node)
    if any(name in DATA_DIRS for name in dirs):
        return True
    loose_exts = DATA_ISH_LOOSE_EXTS_ROOT_MOD if mod_type == "dinput" else DATA_ISH_LOOSE_EXTS_NON_ROOT
    return any(_ext(name) in loose_exts for name in plain)


def _find_override(files: list[str]) -> str | None:
    hits = [p for p in files if p.rsplit("/", 1)[-1].lower() == OVERRIDE_JSON]
    return min(hits, key=lambda p: (p.count("/"), len(p))) if hits else None


def _override_plan(files: list[str], override_path: str, read_text) -> LayoutPlan | None:
    """Build a plan from a mod author's vortex_override_instructions.json."""
    base = override_path.rsplit("/", 1)[0] if "/" in override_path else ""
    try:
        data = json.loads(read_text(override_path))
    except Exception as exc:  # noqa: BLE001 - fall back to normal layout rules
        return LayoutPlan("as_is", [], [f"unreadable {OVERRIDE_JSON}: {exc}"])
    if not isinstance(data, list):
        return None

    index = {p.lower(): p for p in files}
    plan = LayoutPlan("override_json")
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "copy":
            continue
        source = _norm(str(item.get("source") or ""))
        destination = _norm(str(item.get("destination") or "")) or source
        if not source:
            continue
        full = f"{base}/{source}" if base else source
        actual = index.get(full.lower())
        if actual is None:
            plan.warnings.append(f"{OVERRIDE_JSON} source missing from archive: {source}")
            continue
        lowered = destination.lower()
        if lowered == "data" or lowered.startswith("data/"):
            dest = destination[5:]
        else:
            dest = f"Root/{destination}"
        if dest:
            plan.files.append((actual, dest))
    return plan


def plan_layout(
    entries: Iterable[Any], mod_type: str = "", read_text=None
) -> LayoutPlan:
    """Decide where each file of a plain archive goes inside the MO2 mod folder.

    `entries` is the archive listing (relative paths, or objects with
    ``path``/``is_dir``). `mod_type` is the collection manifest's
    ``details.type`` -- ``"dinput"`` means a root-folder mod. `read_text` is an
    optional callable taking an archive-relative path and returning its text; it
    is only used to honour ``vortex_override_instructions.json``.
    """
    files = _paths(entries)
    if not files:
        return LayoutPlan("as_is", [], ["archive contains no files"])

    override_path = _find_override(files)
    if override_path is not None and read_text is not None:
        plan = _override_plan(files, override_path, read_text)
        if plan is not None and plan.files:
            return plan

    # 1. Walk down to the directory that is (or contains) the game's Data folder.
    # Only descend into a single subfolder while doing so still might land on
    # Data; if it dead-ends on an unrecognised folder/file, roll all the way
    # back to the root and install as-is instead of unwrapping blindly (rule B).
    node = ""
    descended = 0
    while True:
        if _looks_like_data(files, node, mod_type):
            data_root, terminal = node, "data"
            break
        dirs, _plain = _children(files, node)
        wrapped = dirs.get("data")
        if wrapped is not None:
            data_root = f"{node}/{wrapped}" if node else wrapped
            terminal = "data_wrapped"
            break
        candidates = [name for key, name in dirs.items() if key != "fomod"]
        if len(candidates) == 1 and descended < MAX_DESCEND:
            node = f"{node}/{candidates[0]}" if node else candidates[0]
            descended += 1
            continue
        # Dead end: this level is neither Data-like nor a single wrapper we
        # can keep unwrapping. Roll back to the root rather than trusting
        # whatever depth we happened to reach (e.g. __MACOSX, MyStuff/Weird).
        node, descended = "", 0
        data_root, terminal = node, "as_is"
        break

    is_root_mod = mod_type == "dinput"
    if is_root_mod and terminal == "as_is":
        # Nothing here belongs in Data at all (imgui.dll, d3dcompiler_47.dll):
        # the whole archive is game-folder content.
        data_root, terminal = None, "root"

    if terminal == "root":
        strategy = "root"
    elif terminal == "data" and descended:
        strategy = "single_folder"
    else:
        strategy = terminal
    # Everything outside the Data folder is measured from its parent, so SKSE's
    # skse64_2_02_06/skse64_loader.exe becomes Root/skse64_loader.exe.
    if data_root is None:
        container = node
    elif "/" in data_root:
        container = data_root.rsplit("/", 1)[0]
    else:
        container = ""

    plan = LayoutPlan(strategy)
    dropped: list[str] = []
    for path in files:
        if data_root is not None and _under(path, data_root):
            rel = _relative(path, data_root)
            if rel:
                plan.files.append((path, rel))
            continue
        outside = _relative(path, container) if _under(path, container) else path
        head, sep, _ = outside.partition("/")
        if not sep and _ext(head) in README_EXTS:
            continue  # documentation, dropped silently
        if head.lower() == "fomod" or head.lower() == OVERRIDE_JSON:
            continue
        if is_root_mod:
            if head.lower() == "src":
                continue  # SKSE ships its own source tree; MO2 does not want it
            plan.files.append((path, f"Root/{outside}"))
        else:
            dropped.append(path)

    if dropped:
        shown = ", ".join(dropped[:12])
        more = f" (+{len(dropped) - 12} more)" if len(dropped) > 12 else ""
        plan.warnings.append(f"dropped {len(dropped)} file(s) outside the mod content: {shown}{more}")
    if strategy == "as_is":
        dirs, plain = _children(files, node)
        plan.warnings.append(
            "could not identify a Data folder; installed as-is "
            f"(top level: {', '.join(sorted(list(dirs.values()) + plain)[:12])})"
        )
    if not plan.files:
        plan.warnings.append("nothing to install")
    return plan
