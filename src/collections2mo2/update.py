"""`c2mo2 update` / `c2mo2 status`: move a collection layer to a newer revision.

A collection is a moving target: the curator publishes revision after revision, each
one a handful of mods different from the last. Re-running `create` would re-download
and re-install all of them; `update` works out the *delta* between the manifest the
instance has installed and the one it is moving to, and touches only that.

    c2mo2 update --instance <dir> [--layer <slug>] [--to <n>|latest] [--dry-run] [--yes]

What the delta is made of (`diff_manifests`):

``unchanged``  the same archive, the same FOMOD answers, the same folder name. Nothing
              is downloaded, extracted or written; the folder simply changes hands to
              `collection:<slug>@<newrev>` in the ledger and in its `meta.ini`.
``changed``   the same mod, a different file / different `choices` / a different
              `phase`. Only a file, `choices` or `hashes` difference costs a
              reinstall; a phase or `optional` flip is a profile-render matter and a
              pure rename is a folder rename.
``added``     download and install, like a small `create`.
``removed``   delete the folder when this layer is its only owner and the folder still
              looks like what we installed; keep it (and warn) when it does not, which
              is how a mod the user has edited by hand survives an update.

Matching old mods to new ones is deliberately not by `source.tag`: Vortex re-issues
every tag on every revision (verified on h2uqa3: 66 -> 68 shares not one tag), so the
tag is tried first only because a curator *may* keep it, and the real work is done by
`(modId, fileId)`, then `md5`, then `modId` alone -- which is what "the same mod, a
new file" looks like in a manifest.

Nothing is written before the plan is printed and confirmed (`--dry-run` stops there,
`--yes` skips the question), and the destructive half -- folder deletes and renames --
runs only after every download and install has succeeded.

`status` is the read-only companion: what each layer is pinned to, what the newest
published revision is, who owns the `mods/` folders, and whether the profile on disk
still matches the ledger. It never writes to the instance.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import archive_inspect, build, create, installer, ledger, profile
from .downloader import run_download
from .manifest import fetch_manifest, load_manifest
from .naming import assign_folder_names
from .nexus import AuthRequired, CollectionRef, NexusClient, NexusError
from .reporter import Reporter, get_reporter

# `meta.ini` is rewritten by every install and by `stamp_owner`, so it can never say
# anything about whether the *user* touched a mod folder.
IGNORED_IN_MOD_DIR = {"meta.ini"}
MAX_LISTED = 30
CHANGELOG_LINES = 40


# -- the delta -------------------------------------------------------------------------


@dataclass
class ModDelta:
    """One mod's fate in an update: what it was, what it becomes, and why."""

    kind: str  # "unchanged" | "changed" | "added" | "removed"
    name: str
    old_tag: str = ""
    new_tag: str = ""
    old_folder: str = ""
    new_folder: str = ""
    reasons: list[str] = field(default_factory=list)
    size: int = 0
    md5: str = ""
    needs_install: bool = False
    # What to do with `old_folder` once the new revision is in place, decided against the
    # ledger before anything is downloaded (`plan_folder_actions`):
    #   "rename"      move it to `new_folder`; the archive did not change
    #   "drop-old"    delete it; the mod was reinstalled under `new_folder`
    #   "release-old" leave it on disk for the layer that also owns it, drop our claim
    folder_action: str = ""

    @property
    def renamed(self) -> bool:
        return bool(self.old_folder and self.new_folder and self.old_folder != self.new_folder)

    def label(self) -> str:
        if self.kind == "changed" and self.renamed:
            return f"{self.old_folder} -> {self.new_folder}"
        return self.name


@dataclass
class ManifestDiff:
    unchanged: list[ModDelta] = field(default_factory=list)
    changed: list[ModDelta] = field(default_factory=list)
    added: list[ModDelta] = field(default_factory=list)
    removed: list[ModDelta] = field(default_factory=list)

    @property
    def install_tags(self) -> set[str]:
        """New-manifest tags that have to be downloaded, inspected and installed."""
        return {d.new_tag for d in [*self.changed, *self.added] if d.needs_install and d.new_tag}

    @property
    def download_bytes(self) -> int:
        return sum(d.size for d in [*self.changed, *self.added] if d.needs_install)

    def counts(self) -> dict[str, int]:
        return {
            "unchanged": len(self.unchanged),
            "changed": len(self.changed),
            "added": len(self.added),
            "removed": len(self.removed),
        }


def _src(mod: dict[str, Any]) -> dict[str, Any]:
    return mod.get("source") or {}


def _md5(mod: dict[str, Any]) -> str:
    return str(_src(mod).get("md5") or "").lower()


def _match_old_mods(
    old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair up the two manifests' mods; returns `(pairs, added_idx, removed_idx)`.

    Tried in order: `source.tag`, `(modId, fileId)`, `md5`, then `modId` alone. Each old
    mod is claimed at most once, so a curator who lists the same mod twice with two files
    still gets two distinct pairings.
    """
    by_tag: dict[str, list[int]] = {}
    by_modfile: dict[tuple, list[int]] = {}
    by_md5: dict[str, list[int]] = {}
    by_modid: dict[Any, list[int]] = {}
    for i, mod in enumerate(old):
        src = _src(mod)
        if src.get("tag"):
            by_tag.setdefault(str(src["tag"]), []).append(i)
        if src.get("modId") is not None and src.get("fileId") is not None:
            by_modfile.setdefault((src["modId"], src["fileId"]), []).append(i)
        if _md5(mod):
            by_md5.setdefault(_md5(mod), []).append(i)
        if src.get("modId") is not None:
            by_modid.setdefault(src["modId"], []).append(i)

    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    added: list[int] = []
    for j, mod in enumerate(new):
        src = _src(mod)
        buckets = (
            by_tag.get(str(src.get("tag") or ""), []),
            by_modfile.get((src.get("modId"), src.get("fileId")), []),
            by_md5.get(_md5(mod), []),
            by_modid.get(src.get("modId"), []),
        )
        hit = next((i for bucket in buckets for i in bucket if i not in used), None)
        if hit is None:
            added.append(j)
        else:
            used.add(hit)
            pairs.append((hit, j))
    removed = [i for i in range(len(old)) if i not in used]
    return pairs, added, removed


def _reasons(old: dict[str, Any], new: dict[str, Any]) -> tuple[list[str], bool]:
    """Why `old` and `new` differ, and whether the difference costs a reinstall."""
    reasons: list[str] = []
    reinstall = False
    old_src, new_src = _src(old), _src(new)
    if old_src.get("fileId") != new_src.get("fileId"):
        reasons.append(f"file {old_src.get('fileId')} -> {new_src.get('fileId')}")
        reinstall = True
    if _md5(old) != _md5(new):
        if not reinstall:
            reasons.append("archive changed")
        reinstall = True
    if (old.get("choices") or None) != (new.get("choices") or None):
        reasons.append("FOMOD choices changed")
        reinstall = True
    if (old.get("hashes") or None) != (new.get("hashes") or None):
        reasons.append("file list changed")
        reinstall = True
    if old.get("phase") != new.get("phase"):
        reasons.append(f"phase {old.get('phase')} -> {new.get('phase')}")
    if bool(old.get("optional")) != bool(new.get("optional")):
        reasons.append(f"optional {bool(old.get('optional'))} -> {bool(new.get('optional'))}")
    if (old.get("version") or "") != (new.get("version") or ""):
        reasons.append(f"version {old.get('version') or '?'} -> {new.get('version') or '?'}")
    return reasons, reinstall


def plan_folder_actions(diff: ManifestDiff, mods_dir: Path, exclusive: set[str]) -> None:
    """Decide what happens to each renamed mod's old folder, before anything is fetched.

    `exclusive` is the set of `mods/` folders this layer is the *sole* owner of. Those we
    may move or delete; a folder another layer also owns is left where it is and the new
    revision's version of the mod is installed fresh under its new name, which is why this
    has to run before `install_tags` is read.
    """
    for delta in diff.changed:
        if not delta.renamed:
            continue
        ours = delta.old_folder in exclusive and (mods_dir / delta.old_folder).is_dir()
        if not ours:
            delta.needs_install = True
            delta.folder_action = "release-old"
            delta.reasons.append(f"installed fresh (mods/{delta.old_folder} is not ours to move)")
        elif delta.needs_install:
            delta.folder_action = "drop-old"
        else:
            delta.folder_action = "rename"


def diff_manifests(
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    *,
    old_folders: dict[str, str] | None = None,
    taken: dict[str, str] | None = None,
    suffix: str = "",
) -> ManifestDiff:
    """Classify every mod of two collection revisions as unchanged/changed/added/removed.

    `old_folders` maps the *old* manifest's tags to the `mods/` folder each one was
    installed into (from the layer's `install.json`); `taken` and `suffix` are handed
    straight to `naming.assign_folder_names` so the new revision's folder names come out
    exactly as the installer would produce them, layer suffixes included.
    """
    old_mods: list[dict[str, Any]] = old_manifest.get("mods") or []
    new_mods: list[dict[str, Any]] = new_manifest.get("mods") or []
    old_folders = old_folders or {}
    new_folders = assign_folder_names(new_mods, taken=taken, suffix=suffix)
    fallback_old = assign_folder_names(old_mods)

    def old_folder_of(mod: dict[str, Any]) -> str:
        tag = str(_src(mod).get("tag") or "")
        return old_folders.get(tag) or fallback_old.get(tag, "")

    pairs, added_idx, removed_idx = _match_old_mods(old_mods, new_mods)
    diff = ManifestDiff()

    for i, j in sorted(pairs, key=lambda p: p[1]):
        old, new = old_mods[i], new_mods[j]
        new_tag = str(_src(new).get("tag") or "")
        old_folder, new_folder = old_folder_of(old), new_folders.get(new_tag, "")
        reasons, reinstall = _reasons(old, new)
        if old_folder and new_folder and old_folder != new_folder:
            reasons.append(f"renamed: {old_folder} -> {new_folder}")
        delta = ModDelta(
            kind="changed" if reasons else "unchanged",
            name=new.get("name") or old.get("name") or "",
            old_tag=str(_src(old).get("tag") or ""),
            new_tag=new_tag,
            old_folder=old_folder,
            new_folder=new_folder,
            reasons=reasons,
            size=int(_src(new).get("fileSize") or 0),
            md5=_md5(new),
            needs_install=reinstall,
        )
        (diff.changed if reasons else diff.unchanged).append(delta)

    for j in added_idx:
        new = new_mods[j]
        new_tag = str(_src(new).get("tag") or "")
        diff.added.append(
            ModDelta(
                kind="added",
                name=new.get("name") or "",
                new_tag=new_tag,
                new_folder=new_folders.get(new_tag, ""),
                reasons=["new in this revision"],
                size=int(_src(new).get("fileSize") or 0),
                md5=_md5(new),
                needs_install=True,
            )
        )

    for i in removed_idx:
        old = old_mods[i]
        diff.removed.append(
            ModDelta(
                kind="removed",
                name=old.get("name") or "",
                old_tag=str(_src(old).get("tag") or ""),
                old_folder=old_folder_of(old),
                reasons=["gone from this revision"],
                md5=_md5(old),
            )
        )
    return diff


# -- "has the user been in here?" ------------------------------------------------------


def _count_files(mod_dir: Path) -> int:
    return sum(
        1 for p in mod_dir.rglob("*") if p.is_file() and p.name.lower() not in IGNORED_IN_MOD_DIR
    )


def _newest_mtime(mod_dir: Path) -> float:
    return max(
        (
            p.stat().st_mtime
            for p in mod_dir.rglob("*")
            if p.is_file() and p.name.lower() not in IGNORED_IN_MOD_DIR
        ),
        default=0.0,
    )


def _as_epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def looks_user_modified(
    mod_dir: Path, entry: dict[str, Any] | None, since: str | None = None
) -> str:
    """Why `mod_dir` no longer looks like what we installed, or `""` if it still does.

    Two cheap checks, both deliberately one-sided so an update never keeps a folder the
    user has not actually touched:

    * **extra files** -- more files than `install.json` recorded for it. Files that went
      *missing* are not a reason to keep anything, so only a surplus counts.
    * **mtime** -- a file newer than the moment the layer was installed. Archive
      extraction restores the archive's own timestamps and `shutil.copy2` preserves
      them, so an untouched mod folder is full of old files no matter when it was
      installed. `meta.ini` is excluded: every install and every owner re-stamp
      rewrites it.
    """
    if not mod_dir.is_dir():
        return ""
    recorded = (entry or {}).get("file_count")
    actual = _count_files(mod_dir)
    if isinstance(recorded, int) and recorded > 0 and actual > recorded:
        return (
            f"{actual - recorded} file(s) more than the install recorded ({actual} vs {recorded})"
        )
    epoch = _as_epoch(since)
    if epoch is not None and _newest_mtime(mod_dir) > epoch:
        return f"contains file(s) modified after the layer was installed ({since})"
    return ""


# -- plan rendering --------------------------------------------------------------------


def _mb(size: int) -> str:
    return f"{size / 1e6:.1f} MB" if size < 1e9 else f"{size / 1e9:.2f} GB"


def _instructions_diff(
    old_manifest: dict, new_manifest: dict, old_rev: Any, new_rev: Any
) -> list[str]:
    old_text = ((old_manifest.get("info") or {}).get("installInstructions") or "").splitlines()
    new_text = ((new_manifest.get("info") or {}).get("installInstructions") or "").splitlines()
    if old_text == new_text:
        return []
    return list(
        difflib.unified_diff(
            old_text, new_text, fromfile=f"revision {old_rev}", tofile=f"revision {new_rev}", n=1
        )
    )


def render_plan(
    diff: ManifestDiff,
    *,
    slug: str,
    name: str,
    old_revision: Any,
    new_revision: Any,
    latest_revision: Any = None,
    changelog: dict[str, Any] | None = None,
    instructions_diff: list[str] | None = None,
    already_downloaded: int = 0,
    keep_notes: dict[str, str] | None = None,
) -> list[str]:
    """The `--dry-run` (and pre-confirmation) plan, as lines. Pure: nothing on disk."""
    counts = diff.counts()
    target = f"{new_revision}"
    if latest_revision is not None and str(latest_revision) == str(new_revision):
        target += " (latest published)"
    lines = [
        f"collection:  {name} ({slug})",
        f"installed:   revision {old_revision}",
        f"target:      revision {target}",
        "",
        "mods: "
        + ", ".join(f"{counts[k]} {k}" for k in ("unchanged", "changed", "added", "removed")),
    ]

    if changelog and (changelog.get("description") or "").strip():
        lines.append("")
        lines.append(
            f"changelog for revision {changelog.get('revisionNumber', new_revision)}"
            + (f" ({changelog.get('createdAt')})" if changelog.get("createdAt") else "")
            + ":"
        )
        body = str(changelog["description"]).strip().splitlines()
        lines.extend(f"  {ln}" for ln in body[:CHANGELOG_LINES])
        if len(body) > CHANGELOG_LINES:
            lines.append(f"  ... +{len(body) - CHANGELOG_LINES} more line(s)")

    if instructions_diff:
        lines.append("")
        lines.append("install instructions changed:")
        lines.extend(f"  {ln.rstrip()}" for ln in instructions_diff[:CHANGELOG_LINES])
        if len(instructions_diff) > CHANGELOG_LINES:
            lines.append(f"  ... +{len(instructions_diff) - CHANGELOG_LINES} more line(s)")

    def block(title: str, deltas: list[ModDelta], marker: str) -> None:
        if not deltas:
            return
        lines.append("")
        lines.append(f"{title} ({len(deltas)}):")
        for delta in deltas[:MAX_LISTED]:
            note = f"  [{'; '.join(delta.reasons)}]" if delta.reasons else ""
            extra = (keep_notes or {}).get(delta.old_folder or delta.new_folder or "", "")
            lines.append(f"  {marker} {delta.label()}{note}{'  ' + extra if extra else ''}")
        if len(deltas) > MAX_LISTED:
            lines.append(f"  ... +{len(deltas) - MAX_LISTED} more")

    block("added", diff.added, "+")
    block("changed", diff.changed, "~")
    block("removed", diff.removed, "-")

    lines.append("")
    to_fetch = len(diff.install_tags)
    lines.append(
        f"download:    {to_fetch} archive(s), up to {_mb(diff.download_bytes)}"
        + (f" ({already_downloaded} already in downloads/)" if already_downloaded else "")
    )
    lines.append(f"reinstall:   {to_fetch} mod folder(s)")
    renames = [d for d in diff.changed if d.renamed]
    if renames:
        lines.append(f"rename:      {len(renames)} mod folder(s)")
    delete = [d for d in diff.removed if not (keep_notes or {}).get(d.old_folder or "")]
    lines.append(f"delete:      {len(delete)} mod folder(s)")
    return lines


# -- file plumbing ---------------------------------------------------------------------


def _clear_readonly(func, path: str, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onexc=_clear_readonly)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _entries_by_tag(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(e.get("tag") or ""): e for e in data.get("entries") or [] if e.get("tag")}


def _refresh_entry(
    entry: dict[str, Any], mod: dict[str, Any], folder: str, *, file_identity: bool = True
) -> dict[str, Any]:
    """Carry an install.json row onto the new revision without reinstalling the mod.

    `file_identity=False` keeps the row's `md5` / `mod_id` / `file_id` pointing at the
    archive that is actually unpacked in `mods/`: with `--allow-missing`, a mod the new
    revision re-pinned to a file Nexus no longer serves keeps the old file on disk, and
    saying otherwise in `install.json` would make the profile writer and a later `remove`
    believe in an archive that was never installed.
    """
    src = _src(mod) if file_identity else {}
    out = dict(entry)
    out.update(
        {
            "name": mod.get("name") or entry.get("name") or "",
            "folder": folder or entry.get("folder") or "",
            "tag": _src(mod).get("tag") or entry.get("tag") or "",
            "md5": src.get("md5") or entry.get("md5") or "",
            "mod_id": src.get("modId", entry.get("mod_id")),
            "file_id": src.get("fileId", entry.get("file_id")),
            "phase": mod.get("phase", entry.get("phase")),
            "optional": bool(mod.get("optional")),
        }
    )
    return out


# -- the update ------------------------------------------------------------------------


def _pick_layer(led: ledger.Ledger, slug: str | None, rep: Reporter) -> dict[str, Any] | None:
    layers = led.data.get("layers") or []
    if not layers:
        rep.warn("this instance has no collection layer to update")
        return None
    if slug:
        layer = led.layer_by_slug(slug)
        if layer is None:
            known = ", ".join(f"{e.get('slug')}@{e.get('revision')}" for e in layers)
            rep.warn(f"{slug} is not a layer of this instance (layers: {known})")
        return layer
    if len(layers) == 1:
        return layers[0]
    known = ", ".join(f"{e.get('slug')}@{e.get('revision')}" for e in layers)
    rep.warn(f"this instance has {len(layers)} layers ({known}); pass --layer <slug>")
    return None


def _parse_to(value: str | None) -> int | None:
    if value is None or str(value).strip().lower() in ("latest", ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--to must be a revision number or 'latest', not {value!r}") from exc


def _confirm(rep: Reporter, assume_yes: bool) -> str:
    """`"yes"`, `"no"` (the user declined) or `"no-tty"` (nobody there to ask)."""
    if assume_yes:
        return "yes"
    if sys.stdin is None or not sys.stdin.isatty():
        rep.warn("not running on a terminal: pass --yes to apply this plan (or --dry-run)")
        return "no-tty"
    try:
        answer = input("apply this update? [y/N] ").strip().lower()
    except EOFError:
        return "no"
    return "yes" if answer in ("y", "yes") else "no"


def _downloaded_md5s(paths: create.Paths, led: ledger.Ledger) -> set[str]:
    """md5s of archives already sitting in `downloads/`, from every layer's downloads.json."""
    have: set[str] = set()
    for layer in led.data.get("layers") or []:
        path = profile.instance_path(paths.out, (layer.get("files") or {}).get("downloads"))
        if path is None or not path.exists():
            continue
        for entry in _read_json(path).get("entries") or []:
            file_path = entry.get("path")
            if entry.get("md5") and file_path and Path(file_path).exists():
                have.add(str(entry["md5"]).lower())
    return have


def cmd_update(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    started = time.monotonic()
    paths = create.Paths(Path(args.instance).expanduser().resolve())
    create.migrate_legacy_instance(paths, rep)
    if not (paths.out / ledger.LEDGER_NAME).exists():
        rep.warn(f"{paths.out} is not a c2mo2 instance ({ledger.LEDGER_NAME} not found)")
        return 2

    load_dotenv()
    api_key = os.environ.get("NEXUS_API_KEY") or None
    if not api_key:
        rep.warn("NEXUS_API_KEY is required (set it in .env; see .env.example)")
        return 2

    led = ledger.load(paths.out)
    create.migrate_instance(paths, led, rep)
    layer = _pick_layer(led, getattr(args, "layer", None), rep)
    if layer is None:
        return 2

    slug = layer.get("slug") or ""
    old_revision = layer.get("revision")
    old_owner = led.layer_owner(layer)
    is_base = led.data["layers"][0] is layer
    ref = CollectionRef.parse(
        f"https://www.nexusmods.com/games/{(led.data.get('game') or {}).get('domain') or 'x'}"
        f"/collections/{slug}"
    )

    # -- resolve the target revision -------------------------------------------------
    rep.stage("resolve")
    client = NexusClient(api_key=api_key)
    try:
        wanted = _parse_to(getattr(args, "to", None))
    except ValueError as exc:
        rep.warn(str(exc))
        return 2
    try:
        info = client.revision_info(ref, wanted)
        latest = client.latest_revision(ref)
    except (AuthRequired, NexusError) as exc:
        rep.warn(f"could not resolve the target revision: {exc}")
        return 1
    new_revision = info.revision_number
    rep.log(f"{info.name} ({slug}): installed {old_revision}, latest published {latest}")
    if str(new_revision) == str(old_revision):
        rep.done("update", f"{slug} is already at revision {new_revision}; nothing to do")
        return 0
    if latest is not None and new_revision < int(latest or 0) and wanted is None:
        rep.warn(f"target revision {new_revision} is not the latest ({latest})")
    try:
        if int(new_revision) < int(old_revision):
            rep.warn(
                f"revision {new_revision} is older than the installed {old_revision}: "
                "this is a downgrade, applied as a delta like any other"
            )
    except (TypeError, ValueError):
        pass

    # -- manifests --------------------------------------------------------------------
    rep.stage("fetch")
    old_manifest_path = profile.instance_path(paths.out, layer.get("manifest"))
    if old_manifest_path is None or not old_manifest_path.exists():
        rep.warn(f"the installed revision's manifest is missing ({old_manifest_path})")
        return 2
    try:
        _, new_manifest_path = fetch_manifest(
            client, ref, new_revision, paths.collections, info=info
        )
    except (AuthRequired, NexusError, OSError, ValueError) as exc:
        rep.warn(f"could not fetch revision {new_revision}: {exc}")
        return 1
    old_manifest = load_manifest(old_manifest_path)
    new_manifest = load_manifest(new_manifest_path)
    rep.done("fetch", f"revision {new_revision} manifest -> {new_manifest_path}")

    lp_old = create.LayerPaths(paths, slug, old_revision)
    lp_new = create.LayerPaths(paths, slug, new_revision)
    old_install = _read_json(lp_old.install_json)
    old_downloads = _read_json(lp_old.downloads_json)
    old_inspect = _read_json(lp_old.inspect_json)
    if not old_install.get("entries"):
        rep.warn(f"the layer's install.json is missing or empty ({lp_old.install_json})")
        return 2

    # -- the delta ---------------------------------------------------------------------
    old_entries = _entries_by_tag(old_install)
    old_folders = {tag: e.get("folder") or "" for tag, e in old_entries.items()}
    # Folders this layer alone owns are ours to reuse and rename; anything another layer
    # also owns is "taken" and a differing archive has to go to a ` ~<slug>` folder.
    mine = set(led.mods_owned_only_by(old_owner))
    taken = {
        folder: (record.get("md5") or "")
        for folder, record in led.data["mods"].items()
        if folder not in mine
    }
    diff = diff_manifests(
        old_manifest,
        new_manifest,
        old_folders=old_folders,
        taken=taken,
        suffix="" if is_base else slug,
    )
    # Renames decide whether a mod is moved or reinstalled, so they have to be settled
    # before `install_tags` is read (and before the plan is printed).
    plan_folder_actions(diff, paths.mods, mine)

    # -- removals: is the folder still ours to delete? ---------------------------------
    keep_notes: dict[str, str] = {}
    for delta in diff.removed:
        folder = delta.old_folder
        if not folder:
            continue
        owners = led.owners_of(folder)
        if [o for o in owners if o != old_owner]:
            keep_notes[folder] = (
                f"(kept: also owned by {', '.join(o for o in owners if o != old_owner)})"
            )
            continue
        why = looks_user_modified(
            paths.mods / folder,
            old_entries.get(delta.old_tag),
            layer.get("updated") or layer.get("added"),
        )
        if why:
            keep_notes[folder] = f"(KEPT as a user mod: {why})"

    changelog = None
    try:
        changelog = client.collection_changelog(ref, new_revision)
    except (AuthRequired, NexusError):
        changelog = None
    plan = render_plan(
        diff,
        slug=slug,
        name=info.name,
        old_revision=old_revision,
        new_revision=new_revision,
        latest_revision=latest,
        changelog=changelog,
        instructions_diff=_instructions_diff(
            old_manifest, new_manifest, old_revision, new_revision
        ),
        already_downloaded=sum(
            1
            for d in [*diff.changed, *diff.added]
            if d.needs_install and d.md5 in _downloaded_md5s(paths, led)
        ),
        keep_notes=keep_notes,
    )
    rep.stage("plan")
    for line in plan:
        rep.log(line)

    if args.dry_run:
        rep.done(
            "update",
            f"dry run: nothing in {paths.out.name} was changed "
            f"(revision {new_revision}'s manifest was cached for the diff)",
        )
        return 0
    answer = _confirm(rep, args.yes)
    if answer != "yes":
        rep.done("update", "cancelled; nothing was changed")
        return 0 if answer == "no" else 2

    # -- download / inspect / install the delta ----------------------------------------
    install_tags = diff.install_tags
    delta_mods = [
        mod
        for mod in new_manifest.get("mods") or []
        if str(_src(mod).get("tag") or "") in install_tags
    ]
    missing_names: list[str] = []
    fresh_downloads: dict[str, dict[str, Any]] = {}
    fresh_inspect: dict[str, dict[str, Any]] = {}
    fresh_install: dict[str, dict[str, Any]] = {}

    tmp_manifest = paths.stage / f"{lp_new.prefix}.delta-manifest.json"
    tmp_downloads = paths.stage / f"{lp_new.prefix}.delta-downloads.json"
    tmp_inspect = paths.stage / f"{lp_new.prefix}.delta-inspect.json"
    tmp_install = paths.stage / f"{lp_new.prefix}.delta-install.json"
    scratch = [tmp_manifest, tmp_downloads, tmp_inspect, tmp_install]

    if delta_mods:
        _write_json(tmp_manifest, {**new_manifest, "mods": delta_mods})
        rep.stage("download")
        try:
            rc = run_download(
                manifest_path=tmp_manifest,
                out_dir=paths.downloads,
                jobs=args.jobs,
                limit=None,
                include_optional=True,
                api_key=api_key,
                json_path=tmp_downloads,
                reporter=rep,
            )
        except (AuthRequired, NexusError) as exc:
            rep.warn(f"download failed: {exc}")
            return 1
        if rc != 0:
            unavailable, mismatched = create._download_failures(tmp_downloads)
            if not args.allow_missing or mismatched or not unavailable:
                rep.warn("one or more archives failed to download; nothing was changed")
                return 1
            rep.warn(
                f"{len(unavailable)} archive(s) are not available from Nexus (--allow-missing):"
            )
            for name, error in unavailable:
                rep.warn(f"  {name}: {error}")
            missing_names = [name for name, _ in unavailable]

        rep.stage("inspect")
        rc = archive_inspect.cmd_inspect(
            argparse.Namespace(
                downloads_json=str(tmp_downloads), out=str(tmp_inspect), jobs=args.jobs
            ),
            reporter=rep,
        )
        if rc != 0:
            rep.warn("one or more archives could not be listed; nothing was changed")
            return 1
        fresh_downloads = _entries_by_tag(_read_json(tmp_downloads))
        fresh_inspect = {
            str(e.get("tag") or ""): e for e in _read_json(tmp_inspect).get("entries") or []
        }

    new_owner = ledger.collection_owner(slug, new_revision)
    if install_tags:
        rep.stage("install")
        rc = installer.cmd_install(
            argparse.Namespace(
                inspect_json=str(tmp_inspect),
                manifest=str(new_manifest_path),
                mods_dir=str(paths.mods),
                only=None,
                game_path=(led.data.get("game") or {}).get("source_path") or None,
                jobs=args.jobs,
                force=True,
                choices_overrides=getattr(args, "choices_overrides", None),
                out=str(tmp_install),
                owner=new_owner,
                taken_folders=taken,
                folder_suffix="" if is_base else slug,
            ),
            reporter=rep,
        )
        if rc != 0:
            rep.warn(
                f"one or more mods failed to install (see {tmp_install.name}); the ledger and "
                "the profile were left as they were"
            )
            return 1
        fresh_install = _entries_by_tag(_read_json(tmp_install))

    # -- folder renames and deletions: only now that every install has succeeded --------
    renamed: list[str] = []
    for delta in diff.changed:
        if not delta.folder_action:
            continue
        src_dir, dest_dir = paths.mods / delta.old_folder, paths.mods / delta.new_folder
        if delta.folder_action == "rename":
            if not src_dir.is_dir() or dest_dir.exists():
                continue
            try:
                src_dir.rename(dest_dir)
            except OSError as exc:
                rep.warn(f"could not rename mods/{delta.old_folder} -> {delta.new_folder}: {exc}")
                continue
            renamed.append(f"{delta.old_folder} -> {delta.new_folder}")
        elif delta.folder_action == "drop-old" and dest_dir.is_dir():
            # The mod was reinstalled under its new name; the old folder is ours and stale.
            _rmtree(src_dir)
        remaining = led.remove_mod_owner(delta.old_folder, old_owner)
        if remaining and src_dir.is_dir():
            installer.stamp_owner(src_dir, ", ".join(remaining))

    # -- removals ----------------------------------------------------------------------
    deleted: list[str] = []
    kept: list[str] = []
    for delta in diff.removed:
        folder = delta.old_folder
        if not folder:
            continue
        remaining = led.remove_mod_owner(folder, old_owner)
        target = paths.mods / folder
        if remaining:
            kept.append(folder)
            if target.is_dir():
                installer.stamp_owner(target, ", ".join(remaining))
            continue
        note = keep_notes.get(folder, "")
        if note.startswith("(KEPT"):
            kept.append(folder)
            rep.warn(f"mods/{folder} looks modified since it was installed; kept as a user mod")
            continue
        _rmtree(target)
        deleted.append(folder)

    # -- the new revision's stage JSON -------------------------------------------------
    old_download_rows = _entries_by_tag(old_downloads)
    old_inspect_rows = {str(e.get("tag") or ""): e for e in old_inspect.get("entries") or []}
    old_by_old_tag = old_entries
    tag_map = {d.new_tag: d.old_tag for d in [*diff.unchanged, *diff.changed] if d.old_tag}

    downloads_out: list[dict[str, Any]] = []
    inspect_out: list[dict[str, Any]] = []
    install_out: list[dict[str, Any]] = []
    for mod in new_manifest.get("mods") or []:
        tag = str(_src(mod).get("tag") or "")
        old_tag = tag_map.get(tag, "")
        if tag in fresh_downloads:
            downloads_out.append(fresh_downloads[tag])
        elif old_tag in old_download_rows:
            row = dict(old_download_rows[old_tag])
            row["tag"] = tag
            downloads_out.append(row)
        if tag in fresh_inspect:
            inspect_out.append(fresh_inspect[tag])
        elif old_tag in old_inspect_rows:
            row = dict(old_inspect_rows[old_tag])
            row["tag"] = tag
            inspect_out.append(row)
        if tag in fresh_install:
            install_out.append(fresh_install[tag])
        elif old_tag in old_by_old_tag:
            delta = next((d for d in [*diff.unchanged, *diff.changed] if d.new_tag == tag), None)
            folder = delta.new_folder if delta else old_by_old_tag[old_tag].get("folder", "")
            # A mod we meant to reinstall but could not (--allow-missing) keeps the
            # archive it actually has on disk, warning and all.
            stale = bool(delta and delta.needs_install)
            row = _refresh_entry(old_by_old_tag[old_tag], mod, folder, file_identity=not stale)
            if stale:
                note = (
                    f"revision {new_revision} re-pinned this mod to a file Nexus would not "
                    f"serve; the revision {old_revision} archive is still installed"
                )
                row["warnings"] = [*(row.get("warnings") or []), note]
            install_out.append(row)

    _write_json(
        lp_new.downloads_json,
        {
            "manifest": str(new_manifest_path.resolve()),
            "domain": old_downloads.get("domain") or (led.data.get("game") or {}).get("domain"),
            "game_name": old_downloads.get("game_name")
            or (led.data.get("game") or {}).get("mo2_name"),
            "entries": downloads_out,
        },
    )
    _write_json(
        lp_new.inspect_json,
        {"downloads_json": str(lp_new.downloads_json.resolve()), "entries": inspect_out},
    )
    _write_json(
        lp_new.install_json,
        {
            "manifest": str(new_manifest_path.resolve()),
            "mods_dir": str(paths.mods),
            "game_name": old_install.get("game_name") or "",
            "entries": install_out,
        },
    )
    if lp_old.survey_json.exists() and not lp_new.survey_json.exists():
        shutil.copy2(lp_old.survey_json, lp_new.survey_json)

    # -- ownership: every surviving folder changes hands to the new revision ------------
    for entry in install_out:
        folder = entry.get("folder")
        if not folder:
            continue
        others = [
            o for o in led.owners_of(folder) if o not in (old_owner, new_owner, ledger.USER_OWNER)
        ]
        led.remove_mod_owner(folder, old_owner)
        led.set_mod_owner(
            folder,
            new_owner,
            tag=entry.get("tag") or "",
            md5=entry.get("md5") or "",
            install_mode=entry.get("install_mode") or "",
            strategy=entry.get("strategy") or "",
            plugins=entry.get("plugins") or [],
        )
        for other in others:
            led.add_mod_owner(folder, other)
        if (paths.mods / folder).is_dir():
            installer.stamp_owner(paths.mods / folder, ", ".join(led.owners_of(folder)))

    # -- INI keys the old revision set, before the new one re-applies its own -----------
    profile_name = layer.get("profile") or ""
    profile_dir = paths.out / "profiles" / profile_name if profile_name else None
    if profile_dir is not None and profile_dir.is_dir():
        for note in profile.revert_ini_keys(profile_dir, led.ini_keys_of(old_owner)):
            rep.log(note)
    led.drop_ini_keys(old_owner)

    # -- the layer record ---------------------------------------------------------------
    manifest_rel = new_manifest_path.resolve().relative_to(paths.out.resolve()).as_posix()
    led.update_layer_revision(
        slug,
        old_revision,
        new_revision,
        name=(new_manifest.get("info") or {}).get("name") or info.name,
        author=(new_manifest.get("info") or {}).get("author") or "",
        manifest=manifest_rel,
        files=lp_new.ledger_files(),
    )
    led.normalise_owner_order()
    led.save()

    # -- profile ------------------------------------------------------------------------
    game = led.data.get("game") or {}
    was_separator = set(layer.get("separators") or [])
    try:
        report = create.render_profile(
            paths,
            led,
            rep,
            game_path=game.get("stock_game_dir") or game.get("source_path") or "",
            mo2_version=(led.data.get("mo2") or {}).get("version") or build.DEFAULT_MO2_VERSION,
        )
    except (OSError, ValueError) as exc:
        rep.warn(f"profile could not be re-rendered: {exc}")
        return 1

    # A revision that empties a phase leaves its separator folder behind. The re-rendered
    # modlist.txt no longer lists it, but MO2 puts every unlisted `mods/` folder back on
    # the next launch, so an empty one nobody owns any more has to go.
    dropped_separators: list[str] = []
    for name in sorted(was_separator - led.separator_folders()):
        sep_dir = paths.mods / name
        if not sep_dir.is_dir():
            continue
        if any(p.name.lower() != "meta.ini" for p in sep_dir.rglob("*")):
            rep.warn(f"mods/{name} is no longer a separator this instance uses, but is not empty")
            continue
        _rmtree(sep_dir)
        dropped_separators.append(name)

    # -- tidy up -------------------------------------------------------------------------
    for path in scratch:
        if path.exists():
            path.unlink()
    stale: list[str] = []
    if str(old_revision) != str(new_revision):
        for key in create.STAGE_FILES:
            path = getattr(lp_old, key)
            if path.exists():
                path.unlink()
                stale.append(path.name)
    purged = ""
    if getattr(args, "purge_old", False):
        old_dir = paths.collections / slug / str(old_revision)
        _rmtree(old_dir)
        purged = str(old_dir)

    counts = diff.counts()
    rep.stage("summary")
    rep.log(f"  {slug}: revision {old_revision} -> {new_revision}")
    rep.log(f"  unchanged:          {counts['unchanged']:>4}")
    rep.log(f"  changed:            {counts['changed']:>4} ({len(install_tags)} reinstalled)")
    rep.log(f"  added:              {counts['added']:>4}")
    rep.log(f"  removed:            {counts['removed']:>4} ({len(deleted)} folder(s) deleted)")
    rep.log(f"  folders renamed:    {len(renamed):>4}")
    for line in renamed[:MAX_LISTED]:
        rep.log(f"    {line}")
    replaced = [d for d in diff.changed if d.folder_action == "drop-old"]
    rep.log(f"  folders replaced:   {len(replaced):>4} (renamed *and* reinstalled)")
    for delta in replaced[:MAX_LISTED]:
        rep.log(f"    {delta.old_folder} -> {delta.new_folder}")
    if dropped_separators:
        rep.log(
            f"  separators dropped: {len(dropped_separators):>4} ({', '.join(dropped_separators)})"
        )
    rep.log(f"  folders kept:       {len(kept):>4}")
    for folder in kept[:MAX_LISTED]:
        rep.log(f"    {folder} {keep_notes.get(folder, '')}")
    if missing_names:
        rep.log(f"  NOT installed (unavailable on Nexus): {len(missing_names)}")
        for name in missing_names:
            rep.log(f"    {name}")
    rep.log(f"  stage files removed:{len(stale):>4} ({', '.join(stale) or 'none'})")
    if purged:
        rep.log(f"  old manifest purged: {purged}")
    else:
        rep.log(f"  old manifest kept:  {paths.collections / slug / str(old_revision)}")
    rep.log(f"  modlist entries:    {len(report.get('mod_order') or [])}")
    rep.log(f"  user mods kept:     {len(report.get('user_mods') or [])}")
    rep.log(f"  elapsed: {time.monotonic() - started:.1f}s")
    rep.done("update", f"{slug} is now at revision {new_revision}")
    return 0


# -- status ------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    """Read-only: what this instance is made of and whether a newer revision exists."""
    rep = get_reporter(reporter)
    paths = create.Paths(Path(args.instance).expanduser().resolve())
    # The only thing `status` ever writes: renaming a pre-rename instance's own files.
    create.migrate_legacy_instance(paths, rep)
    if not (paths.out / ledger.LEDGER_NAME).exists():
        rep.warn(f"{paths.out} is not a c2mo2 instance ({ledger.LEDGER_NAME} not found)")
        return 2

    load_dotenv()
    led = ledger.load(paths.out)  # never saved: `status` must not write to the instance
    layers = led.data.get("layers") or []
    game = led.data.get("game") or {}

    rep.stage("status")
    rep.log(f"instance:  {paths.out}")
    rep.log(f"game:      {game.get('mo2_name') or '?'} ({game.get('domain') or '?'})")
    rep.log(f"           {game.get('stock_game_dir') or game.get('source_path') or '?'}")
    rep.log(f"MO2:       {(led.data.get('mo2') or {}).get('version') or '?'}")

    client = None
    if not getattr(args, "offline", False):
        client = NexusClient(api_key=os.environ.get("NEXUS_API_KEY") or None)

    owners = led.scan_mods_dir(paths.mods)
    rep.log("")
    rep.log(f"layers ({len(layers)}):")
    for i, layer in enumerate(layers):
        slug = layer.get("slug") or "?"
        revision = layer.get("revision")
        owner = led.layer_owner(layer)
        latest: Any = "?"
        if client is not None:
            try:
                latest = client.latest_revision(
                    CollectionRef.parse(
                        f"https://www.nexusmods.com/games/{game.get('domain') or 'x'}"
                        f"/collections/{slug}"
                    )
                )
            except (AuthRequired, NexusError, OSError) as exc:
                latest = f"? ({exc})"
        state = "up to date" if str(latest) == str(revision) else f"update available -> {latest}"
        role = "base" if i == 0 else "add-on"
        rep.log(f"  [{role}] {layer.get('name') or slug} ({slug})")
        rep.log(f"     installed revision {revision}, latest published {latest}: {state}")
        previous = layer.get("previous_revisions") or []
        if previous:
            rep.log(f"     previously: {', '.join(str(p) for p in previous)}")
        # `scan_mods_dir` attributes a layer's separator folders to it; they are instance
        # furniture, not mods, so count them apart.
        seps = set(layer.get("separators") or [])
        owned = [f for f, o in owners.items() if owner in o and f not in seps]
        shared = [f for f in owned if owners[f] != [owner]]
        rep.log(
            f"     mods on disk: {len(owned)} ({len(shared)} shared with other layers), "
            f"{len(seps & set(owners))} separator(s)"
        )
        if layer.get("updated"):
            rep.log(f"     updated: {layer['updated']}")

    separators = led.separator_folders() & set(owners)
    user_mods = [f for f, o in owners.items() if o == [ledger.USER_OWNER]]
    tool_mods = [f for f, o in owners.items() if any(str(x).startswith("tool:") for x in o)]
    collection_mods = [
        f
        for f, o in owners.items()
        if f not in separators and any(str(x).startswith("collection:") for x in o)
    ]
    rep.log("")
    rep.log(f"mods/ folders: {len(owners)} total")
    rep.log(f"  owned by a collection layer: {len(collection_mods)}")
    rep.log(f"  separators:                  {len(separators)}")
    rep.log(f"  installed by a tool:         {len(tool_mods)}")
    rep.log(f"  user mods (unowned):         {len(user_mods)}")
    for folder in user_mods[:MAX_LISTED]:
        rep.log(f"    {folder}")
    if len(user_mods) > MAX_LISTED:
        rep.log(f"    ... +{len(user_mods) - MAX_LISTED} more")

    tools = led.data.get("tools") or {}
    rep.log("")
    rep.log(f"tools ({len(tools)}): {', '.join(sorted(tools)) or 'none'}")

    in_sync = create.profile_is_current(paths, led)
    report = _read_json(paths.profile_report)
    rep.log("")
    rep.log(f"profile:   {report.get('profile') or '?'}")
    rep.log(
        f"           {'in sync with the ledger' if in_sync else 'OUT OF SYNC: re-render it (c2mo2 update / add / remove)'}"
    )
    rep.done("status", f"{len(layers)} layer(s), {len(owners)} mod folder(s)")
    return 0


# -- parsers -------------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    u = subparsers.add_parser(
        "update",
        help="move a collection layer to a newer revision, applying only the delta",
    )
    u.add_argument("--instance", required=True, help="the instance directory")
    u.add_argument(
        "--layer",
        default=None,
        help="the collection slug to update (default: the only layer, if there is one)",
    )
    u.add_argument(
        "--to",
        default=None,
        help="target revision number, or 'latest' (default: latest published)",
    )
    u.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="print the plan and exit without touching anything",
    )
    u.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="apply the plan without asking (required when there is no terminal)",
    )
    u.add_argument("--jobs", type=int, default=4, help="parallel workers per stage (default: 4)")
    u.add_argument(
        "--allow-missing",
        action="store_true",
        default=False,
        help="carry on when Nexus no longer serves a file the new revision pins",
    )
    u.add_argument(
        "--purge-old",
        action="store_true",
        default=False,
        help="delete the old revision's manifest folder too (it is kept by default, "
        "for diffing and for going back)",
    )
    u.add_argument(
        "--choices-overrides",
        default=None,
        help='JSON file of {"<tag>": <Vortex choices object>} for fresh-mode FOMODs',
    )
    u.set_defaults(func=cmd_update)

    s = subparsers.add_parser(
        "status",
        help="what an instance is made of, and whether a newer revision is published",
    )
    s.add_argument("--instance", required=True, help="the instance directory")
    s.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="skip the Nexus lookups (no 'latest revision' column)",
    )
    s.set_defaults(func=cmd_status)
