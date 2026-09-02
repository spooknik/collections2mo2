"""`<instance>/c2wj-instance.json`: who put what into this MO2 instance.

An instance is not one collection forever: a second collection can be layered on
top, tools get added, and the user installs their own mods by hand. Nothing in
MO2's own files records where a `mods/<folder>` came from, so we keep our own
ledger next to `ModOrganizer.exe`:

    version     schema version of this file (currently 1)
    created     / updated: ISO 8601 UTC timestamps
    game        {domain, mo2_name, source_path, stock_game_dir|null}
    mo2         {version, rootbuilder_version}
    layers      one entry per collection revision applied, in application order
    mods        folder name -> {owner, tag, md5, install_mode, strategy, plugins}
    tools       tool id -> record (written by the `tools` command)
    ini_keys    owner -> {ini file -> {section -> [keys this owner set]}}

`owner` is `"collection:<slug>@<rev>"`, `"tool:<id>"` or `"user"`. A folder in
`mods/` that the ledger has never heard of is a user mod (`owners_of` says so),
which is what makes it safe to remove a layer later: only folders owned by that
layer, and by nobody else, can go.

Writes are atomic (temp file in the same directory, then `os.replace`) because
this file is the only record of the above and the pipeline updates it while a
long install is in flight.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_NAME = "c2wj-instance.json"
VERSION = 1

USER_OWNER = "user"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def collection_owner(slug: str, revision: int | str) -> str:
    """The owner string for a collection revision: `collection:<slug>@<rev>`."""
    return f"collection:{slug}@{revision}"


def tool_owner(tool_id: str) -> str:
    return f"tool:{tool_id}"


def _empty(now: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "created": now,
        "updated": now,
        "game": {"domain": None, "mo2_name": None, "source_path": None, "stock_game_dir": None},
        "mo2": {"version": None, "rootbuilder_version": None},
        "layers": [],
        "mods": {},
        "tools": {},
        "ini_keys": {},
    }


class Ledger:
    """The parsed `c2wj-instance.json` of one MO2 instance."""

    def __init__(self, instance_dir: Path, data: dict[str, Any] | None = None):
        self.instance_dir = Path(instance_dir)
        self.path = self.instance_dir / LEDGER_NAME
        self.data: dict[str, Any] = data if data is not None else _empty(now_iso())
        # Tolerate a hand-edited or older file that is missing whole sections.
        for key, default in _empty(self.data.get("created") or now_iso()).items():
            self.data.setdefault(key, default)

    # -- persistence -------------------------------------------------------------

    def save(self) -> Path:
        """Write the ledger atomically (temp file + `os.replace`)."""
        self.data["updated"] = now_iso()
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # -- sections ----------------------------------------------------------------

    def set_game(
        self,
        domain: str | None = None,
        mo2_name: str | None = None,
        source_path: str | None = None,
        stock_game_dir: str | None = None,
    ) -> None:
        game = self.data["game"]
        if domain is not None:
            game["domain"] = domain
        if mo2_name is not None:
            game["mo2_name"] = mo2_name
        if source_path is not None:
            game["source_path"] = source_path
        # None is meaningful here (no stock game copy), so always write it.
        game["stock_game_dir"] = stock_game_dir

    def set_mo2(self, version: str | None = None, rootbuilder_version: str | None = None) -> None:
        if version is not None:
            self.data["mo2"]["version"] = version
        if rootbuilder_version is not None:
            self.data["mo2"]["rootbuilder_version"] = rootbuilder_version

    def register_layer(
        self,
        slug: str,
        revision: int | str,
        *,
        name: str = "",
        author: str = "",
        profile: str = "",
        separators: list[str] | None = None,
        manifest: str = "",
    ) -> dict[str, Any]:
        """Add (or refresh) the layer for `slug@revision`; returns the layer record.

        `manifest` should be a path relative to the instance directory so the
        instance stays movable.
        """
        layer = {
            "slug": slug,
            "revision": revision,
            "name": name,
            "author": author,
            "added": now_iso(),
            "profile": profile,
            "separators": list(separators or []),
            "manifest": manifest,
        }
        for i, existing in enumerate(self.data["layers"]):
            if existing.get("slug") == slug and str(existing.get("revision")) == str(revision):
                layer["added"] = existing.get("added") or layer["added"]
                self.data["layers"][i] = layer
                return layer
        self.data["layers"].append(layer)
        return layer

    def layer(self, slug: str, revision: int | str) -> dict[str, Any] | None:
        for existing in self.data["layers"]:
            if existing.get("slug") == slug and str(existing.get("revision")) == str(revision):
                return existing
        return None

    def set_mod_owner(
        self,
        folder: str,
        owner: str,
        *,
        tag: str = "",
        md5: str = "",
        install_mode: str = "",
        strategy: str = "",
        plugins: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record that `folder` in `mods/` belongs to `owner`."""
        record = {
            "owner": owner,
            "tag": tag,
            "md5": md5,
            "install_mode": install_mode,
            "strategy": strategy,
            "plugins": list(plugins or []),
        }
        self.data["mods"][folder] = record
        return record

    def owners_of(self, folder: str) -> list[str]:
        """Owners of a `mods/` folder; `["user"]` for anything the ledger does not know."""
        record = self.data["mods"].get(folder)
        if not record:
            return [USER_OWNER]
        owner = record.get("owner") or USER_OWNER
        extra = [o for o in (record.get("also_owned_by") or []) if o != owner]
        return [owner, *extra]

    def record_ini_keys(self, owner: str, keys: dict[str, dict[str, list[str]]]) -> None:
        """Merge `{ini file: {section: [key, ...]}}` into what `owner` has set."""
        if not keys:
            self.data["ini_keys"].setdefault(owner, {})
            return
        bucket = self.data["ini_keys"].setdefault(owner, {})
        for ini_name, sections in keys.items():
            ini_bucket = bucket.setdefault(ini_name, {})
            for section, names in sections.items():
                merged = list(ini_bucket.get(section) or [])
                lowered = {n.lower() for n in merged}
                for name in names:
                    if name.lower() not in lowered:
                        merged.append(name)
                        lowered.add(name.lower())
                ini_bucket[section] = merged

    # -- queries -----------------------------------------------------------------

    def mods_owned_by(self, owner: str) -> list[str]:
        return sorted(f for f, r in self.data["mods"].items() if r.get("owner") == owner)

    def separator_folders(self) -> set[str]:
        """Separator folders any layer created; they are instance furniture, not mods."""
        names: set[str] = set()
        for layer in self.data["layers"]:
            names.update(layer.get("separators") or [])
        return names

    def scan_mods_dir(self, mods_dir: Path | None = None) -> dict[str, list[str]]:
        """`mods/` folder -> owners, for what is actually on disk right now.

        Folders the ledger has no record of come back as `["user"]`; separators
        created by a layer are attributed to that layer rather than to the user.
        """
        mods_dir = Path(mods_dir) if mods_dir else self.instance_dir / "mods"
        if not mods_dir.is_dir():
            return {}
        separators = {}
        for layer in self.data["layers"]:
            owner = collection_owner(layer.get("slug") or "", layer.get("revision"))
            for name in layer.get("separators") or []:
                separators[name] = owner
        out: dict[str, list[str]] = {}
        for child in sorted(mods_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name in separators and child.name not in self.data["mods"]:
                out[child.name] = [separators[child.name]]
            else:
                out[child.name] = self.owners_of(child.name)
        return out


def load(instance_dir: Path | str) -> Ledger:
    """Read `<instance_dir>/c2wj-instance.json`, or start a fresh ledger if absent."""
    instance_dir = Path(instance_dir)
    path = instance_dir / LEDGER_NAME
    if not path.exists():
        return Ledger(instance_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Ledger(instance_dir)
    if not isinstance(data, dict):
        return Ledger(instance_dir)
    return Ledger(instance_dir, data)


def save(ledger: Ledger) -> Path:
    """Module-level alias for `Ledger.save`."""
    return ledger.save()
