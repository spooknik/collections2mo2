"""`<instance>/c2mo2-instance.json`: who put what into this MO2 instance.

An instance is not one collection forever: a second collection can be layered on
top, tools get added, and the user installs their own mods by hand. Nothing in
MO2's own files records where a `mods/<folder>` came from, so we keep our own
ledger next to `ModOrganizer.exe`:

    version     schema version of this file (currently 1)
    created     / updated: ISO 8601 UTC timestamps
    game        {domain, mo2_name, source_path, stock_game_dir|null}
    mo2         {version, rootbuilder_version}
    layers      one entry per collection revision applied, in application order
    mods        folder name -> {owner, owners, tag, md5, install_mode, strategy, plugins}
    tools       tool id -> record (written by the `tools` command)
    ini_keys    owner -> {ini file -> {section -> {key -> {value, previous}}}}
    display     {resolution, vsync, window, updated} -- the last explicit --resolution/
                --vsync/--window the user asked for, re-applied on every render that is
                given 'keep' for a field (see `profile.render_instance`); 'keep' never
                clears it, only `profile-instance --forget-display` does

`owner` is `"collection:<slug>@<rev>"`, `"tool:<id>"` or `"user"`. A folder in
`mods/` that the ledger has never heard of is a user mod (`owners_of` says so),
which is what makes it safe to remove a layer later: only folders owned by that
layer, and by nobody else, can go.

Layering (schema 2) made two of those fields plural:

* `mods[folder]["owners"]` is the full owner list; `owners[0]` is the primary and
  stays mirrored in `owner` so older readers keep working. Two collections that
  pin the *same* archive (SKSE, Address Library, ...) share one folder and both
  appear in `owners`; removing one layer only drops it from the list.
* `ini_keys[owner][ini][section][key]` records `{"value", "previous"}` -- what the
  layer set and what was there before it. `remove` restores `previous`, or deletes
  the key when the layer introduced it.

Each layer also records the stage JSON it produced (`files`), named per layer
(`c2mo2/<slug>-<rev>.install.json` and friends) so layers never clobber each other.

A layer is re-pinned rather than replaced when the collection publishes a new
revision (`update_layer_revision`, used by `c2mo2 update`): it keeps its position in
`layers`, its `added` timestamp and its `profile`, gains an `updated` timestamp, and
appends the revision it left to `previous_revisions`.

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

LEDGER_NAME = "c2mo2-instance.json"
# Pre-rename name (the project was `collections2wabbajack`, CLI `c2wj`). `load`
# still reads it so an instance built before the rename opens; `create.migrate_legacy_instance`
# renames it on disk the first time such an instance is opened.
LEGACY_LEDGER_NAME = "c2wj-instance.json"
VERSION = 2

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
        "display": {"resolution": None, "vsync": None, "window": None, "updated": None},
    }


def _owner_list(record: dict[str, Any]) -> list[str]:
    """Owners of a mod record, tolerating the schema-1 `owner` / `also_owned_by` pair."""
    owners = [o for o in (record.get("owners") or []) if o]
    if not owners:
        primary = record.get("owner")
        owners = [primary] if primary else []
        owners.extend(o for o in (record.get("also_owned_by") or []) if o and o != primary)
    seen: set[str] = set()
    out: list[str] = []
    for owner in owners:
        if owner not in seen:
            seen.add(owner)
            out.append(owner)
    return out


def _normalise_ini_section(entries: Any) -> dict[str, dict[str, Any]]:
    """Accept both schema-1 `[key, ...]` and schema-2 `{key: {value, previous}}`."""
    if isinstance(entries, dict):
        return {
            k: (v if isinstance(v, dict) else {"value": v, "previous": None})
            for k, v in entries.items()
        }
    if isinstance(entries, list):
        return {str(k): {"value": None, "previous": None} for k in entries}
    return {}


class Ledger:
    """The parsed `c2mo2-instance.json` of one MO2 instance."""

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
        files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Add (or refresh) the layer for `slug@revision`; returns the layer record.

        Layers keep their list position: the first one is the base collection and the
        rest are add-ons in the order they were added, which is the order the profile
        renders them in. `manifest` and `files` should be paths relative to the
        instance directory so the instance stays movable.
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
            "files": dict(files or {}),
        }
        for i, existing in enumerate(self.data["layers"]):
            if existing.get("slug") == slug and str(existing.get("revision")) == str(revision):
                layer["added"] = existing.get("added") or layer["added"]
                if not layer["files"]:
                    layer["files"] = dict(existing.get("files") or {})
                # An `update` history belongs to the layer, not to one revision of it.
                for key in ("previous_revisions", "updated"):
                    if existing.get(key):
                        layer[key] = existing[key]
                self.data["layers"][i] = layer
                return layer
        self.data["layers"].append(layer)
        return layer

    def update_layer_revision(
        self,
        slug: str,
        old_revision: int | str,
        new_revision: int | str,
        *,
        name: str = "",
        author: str = "",
        manifest: str = "",
        files: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Re-pin an existing layer to `new_revision`, in place; returns the layer record.

        `c2mo2 update` moves a layer forward without changing what it *is*: it keeps its
        position in `layers` (which is the order the profile renders them in), its
        `added` timestamp, its `profile` and the separators it owns, and appends the
        revision it is leaving to `previous_revisions`. Registering a second layer for
        the same slug instead would render the collection twice.
        """
        layer = self.layer(slug, old_revision)
        if layer is None:
            return None
        if str(old_revision) != str(new_revision):
            previous = list(layer.get("previous_revisions") or [])
            previous.append(old_revision)
            layer["previous_revisions"] = previous
        layer["revision"] = new_revision
        layer["updated"] = now_iso()
        if name:
            layer["name"] = name
        if author:
            layer["author"] = author
        if manifest:
            layer["manifest"] = manifest
        if files:
            layer["files"] = dict(files)
        return layer

    def layer(self, slug: str, revision: int | str) -> dict[str, Any] | None:
        for existing in self.data["layers"]:
            if existing.get("slug") == slug and str(existing.get("revision")) == str(revision):
                return existing
        return None

    def layer_by_slug(self, slug: str) -> dict[str, Any] | None:
        """The (last-added) layer for `slug`, whatever revision it is pinned to."""
        for existing in reversed(self.data["layers"]):
            if existing.get("slug") == slug:
                return existing
        return None

    def base_layer(self) -> dict[str, Any] | None:
        """The first layer applied: the collection the instance was created from."""
        layers = self.data["layers"]
        return layers[0] if layers else None

    def drop_layer(self, slug: str, revision: int | str) -> dict[str, Any] | None:
        """Remove the layer record for `slug@revision` (does not touch `mods`)."""
        for i, existing in enumerate(self.data["layers"]):
            if existing.get("slug") == slug and str(existing.get("revision")) == str(revision):
                return self.data["layers"].pop(i)
        return None

    def layer_owner(self, layer: dict[str, Any]) -> str:
        return collection_owner(layer.get("slug") or "", layer.get("revision"))

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
        """Record that `folder` in `mods/` belongs to `owner` (as its primary owner).

        Any other owners already recorded for the folder are kept: two collection
        layers can pin the same archive and then share one folder.
        """
        previous = self.data["mods"].get(folder) or {}
        others = [o for o in _owner_list(previous) if o != owner and o != USER_OWNER]
        record = {
            "owner": owner,
            "owners": [owner, *others],
            "tag": tag,
            "md5": md5,
            "install_mode": install_mode,
            "strategy": strategy,
            "plugins": list(plugins or []),
        }
        self.data["mods"][folder] = record
        return record

    def add_mod_owner(self, folder: str, owner: str) -> list[str]:
        """Add `owner` as a secondary owner of an existing folder; returns the owners."""
        record = self.data["mods"].get(folder)
        if record is None:
            return self.set_mod_owner(folder, owner)["owners"]
        owners = _owner_list(record)
        if owner not in owners:
            owners.append(owner)
        record["owners"] = owners
        record.setdefault("owner", owners[0])
        record.pop("also_owned_by", None)
        return owners

    def remove_mod_owner(self, folder: str, owner: str) -> list[str]:
        """Drop `owner` from a folder's owners; returns the owners that remain.

        An empty result means nobody owns the folder any more and the caller may
        delete it; the record itself is dropped so the folder does not come back as
        a phantom entry.
        """
        record = self.data["mods"].get(folder)
        if record is None:
            return []
        owners = [o for o in _owner_list(record) if o != owner]
        if not owners:
            del self.data["mods"][folder]
            return []
        record["owners"] = owners
        record["owner"] = owners[0]
        record.pop("also_owned_by", None)
        return owners

    def owners_of(self, folder: str) -> list[str]:
        """Owners of a `mods/` folder; `["user"]` for anything the ledger does not know."""
        record = self.data["mods"].get(folder)
        if not record:
            return [USER_OWNER]
        return _owner_list(record) or [USER_OWNER]

    def record_ini_keys(self, owner: str, keys: dict[str, dict[str, Any]]) -> None:
        """Merge `{ini file: {section: {key: {value, previous}}}}` into `owner`'s set.

        A key already recorded for this owner keeps the `previous` value from the
        first time the layer set it: re-rendering the profile re-applies every
        layer's tweaks, and the value a layer overwrote the *first* time is the one
        `remove` has to put back.
        """
        bucket = self.data["ini_keys"].setdefault(owner, {})
        for ini_name, sections in (keys or {}).items():
            ini_bucket = bucket.setdefault(ini_name, {})
            for section, entries in sections.items():
                sec_bucket = _normalise_ini_section(ini_bucket.get(section))
                for key, info in _normalise_ini_section(entries).items():
                    if key in sec_bucket:
                        sec_bucket[key] = {
                            "value": info.get("value"),
                            "previous": sec_bucket[key].get("previous"),
                        }
                    else:
                        sec_bucket[key] = {
                            "value": info.get("value"),
                            "previous": info.get("previous"),
                        }
                ini_bucket[section] = sec_bucket

    def ini_keys_of(self, owner: str) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        """`owner`'s INI keys, normalised to `{ini: {section: {key: {value, previous}}}}`."""
        out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for ini_name, sections in (self.data["ini_keys"].get(owner) or {}).items():
            out[ini_name] = {s: _normalise_ini_section(e) for s, e in sections.items()}
        return out

    def drop_ini_keys(self, owner: str) -> None:
        self.data["ini_keys"].pop(owner, None)

    def set_display(
        self,
        *,
        resolution: str | None = None,
        vsync: str | None = None,
        window: str | None = None,
    ) -> None:
        """Persist an effective (already keep-substituted) display field.

        Each argument is `None` when that field's render call was 'keep' and the ledger
        had nothing stored to fall back on -- 'keep' never clears a stored field, only
        `clear_display` does. A field given a concrete value (whether freshly requested
        or itself remembered from the ledger) is written; `updated` is refreshed only
        when something actually changed.
        """
        if resolution is None and vsync is None and window is None:
            return
        display = self.data.setdefault(
            "display", {"resolution": None, "vsync": None, "window": None, "updated": None}
        )
        if resolution is not None:
            display["resolution"] = resolution
        if vsync is not None:
            display["vsync"] = vsync
        if window is not None:
            display["window"] = window
        display["updated"] = now_iso()

    def clear_display(self) -> None:
        """Forget the stored display choice (`profile-instance --forget-display`)."""
        self.data["display"] = {"resolution": None, "vsync": None, "window": None, "updated": None}

    # -- queries -----------------------------------------------------------------

    def normalise_owner_order(self) -> None:
        """Sort every folder's `owners` by the order its layers were applied.

        Which layer is *primary* is only a presentation detail (the `meta.ini` comment,
        `mods_owned_by(primary_only=True)`), but it should not depend on which layer was
        re-added last, so the earliest layer that owns a folder always comes first.
        """
        rank = {
            collection_owner(layer.get("slug") or "", layer.get("revision")): i
            for i, layer in enumerate(self.data["layers"])
        }
        for record in self.data["mods"].values():
            owners = _owner_list(record)
            if len(owners) < 2:
                continue
            owners.sort(key=lambda o: rank.get(o, len(rank)))
            record["owners"] = owners
            record["owner"] = owners[0]

    def mods_owned_by(self, owner: str, *, primary_only: bool = False) -> list[str]:
        """Folders `owner` owns; by default including ones it shares with other layers."""
        if primary_only:
            return sorted(f for f, r in self.data["mods"].items() if r.get("owner") == owner)
        return sorted(f for f, r in self.data["mods"].items() if owner in _owner_list(r))

    def mods_owned_only_by(self, owner: str) -> list[str]:
        """Folders `owner` is the *sole* owner of: exactly what removing it may delete."""
        return sorted(f for f, r in self.data["mods"].items() if _owner_list(r) == [owner])

    def upgrade(self) -> None:
        """Normalise a schema-1 file in place: `owners` lists and `{value, previous}` INI keys."""
        for record in self.data["mods"].values():
            owners = _owner_list(record)
            if owners:
                record["owners"] = owners
                record["owner"] = owners[0]
            record.pop("also_owned_by", None)
        for sections in self.data["ini_keys"].values():
            for section_map in sections.values():
                for section, entries in list(section_map.items()):
                    section_map[section] = _normalise_ini_section(entries)
        self.data["version"] = VERSION

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
    """Read `<instance_dir>/c2mo2-instance.json`, or start a fresh ledger if absent.

    Falls back to the pre-rename `c2wj-instance.json` so an instance that has not yet
    been through `create.migrate_legacy_instance` still loads.
    """
    instance_dir = Path(instance_dir)
    path = instance_dir / LEDGER_NAME
    if not path.exists():
        legacy = instance_dir / LEGACY_LEDGER_NAME
        if not legacy.exists():
            return Ledger(instance_dir)
        path = legacy
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Ledger(instance_dir)
    if not isinstance(data, dict):
        return Ledger(instance_dir)
    led = Ledger(instance_dir, data)
    if int(data.get("version") or 1) < VERSION:
        # Schema 1 -> 2: single `owner` becomes an `owners` list, INI keys grow their
        # pre-values. Done in memory; it reaches disk on the next save().
        led.upgrade()
    return led


def save(ledger: Ledger) -> Path:
    """Module-level alias for `Ledger.save`."""
    return ledger.save()
