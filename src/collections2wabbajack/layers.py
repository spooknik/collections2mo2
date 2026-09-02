"""`c2wj add` / `c2wj remove`: collection layers on top of an existing instance.

An MO2 instance is not one collection forever. A curator publishes a base list and
then unofficial add-ons that assume it is installed (Gate to Sovngarde and its
add-on packs, say). `add` installs such a collection *into* an instance that
already exists, as a new layer:

    <instance>/mods/                     shared: one MO2 mods tree
    <instance>/downloads/                shared: one archive store
    <instance>/c2wj/<slug>-<rev>.*.json  per layer: what it downloaded and installed
    <instance>/c2wj-instance.json        the ledger: layers, and who owns each folder

Everything the layer installs is owned by `collection:<slug>@<rev>`. A folder name
that collides with a mod an earlier layer installed is *shared* when both layers
pinned the same archive (same md5) -- SKSE, Address Library and the like -- and gets
a ` ~<slug>` folder of its own when they did not. The profile is then re-rendered
from every layer at once (`profile.render_instance`).

`remove` is the inverse and is careful about three things: folders shared with
another layer are kept (the layer is just dropped from their owner list), the user's
own mods and their place in the load order are never touched, and INI keys the layer
set are put back to the value they had before it (recorded per key in the ledger).
Downloaded archives stay in `downloads/` unless `--purge-downloads`: they cost disk,
not correctness, and a Wabbajack compile ignores what no mod references.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import build, create, installer, ledger, profile
from .manifest import load_manifest
from .nexus import CollectionRef
from .reporter import Reporter, get_reporter


def _instance_paths(instance_dir: str) -> create.Paths:
    return create.Paths(Path(instance_dir).expanduser().resolve())


def _clear_readonly(func, path: str, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onexc=_clear_readonly)


# -- add ------------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    run = create.Run(rep)
    started = time.monotonic()

    paths = _instance_paths(args.instance)
    if not (paths.out / ledger.LEDGER_NAME).exists():
        rep.warn(
            f"{paths.out} is not a c2wj instance ({ledger.LEDGER_NAME} not found). "
            "Use `c2wj create` to make one first."
        )
        return 2

    load_dotenv()
    api_key = os.environ.get("NEXUS_API_KEY") or None
    if not api_key:
        rep.warn("NEXUS_API_KEY is required (set it in .env; see .env.example)")
        return 2

    led = ledger.load(paths.out)
    create.migrate_instance(paths, led, rep)
    if not led.data.get("layers"):
        rep.warn(f"{paths.out} has no base collection layer; run `c2wj create` first")
        return 2

    game = led.data.get("game") or {}
    game_path = Path(args.game_path or game.get("source_path") or "")
    if not game_path.is_dir():
        rep.warn(
            f"game path {game_path} is not a directory; pass --game-path (the ledger says "
            f"{game.get('source_path')!r})"
        )
        return 2

    base = led.data["layers"][0]
    slug = CollectionRef.parse(args.url).slug
    if any(layer.get("slug") == slug for layer in led.data["layers"]):
        rep.log(f"{slug} is already a layer of this instance; re-running it as a refresh")

    paths.c2wj.mkdir(parents=True, exist_ok=True)
    before = set(led.data["mods"])

    ctx = create.add_layer(
        paths, args, led=led, api_key=api_key, game_path=game_path, run=run, rep=rep
    )
    if ctx is None:
        led.save()
        return create._finish(run, rep, paths, started, "add")

    led.save()

    try:
        report = create.render_profile(
            paths,
            led,
            rep,
            game_path=game.get("stock_game_dir") or str(game_path),
            mo2_version=(led.data.get("mo2") or {}).get("version") or build.DEFAULT_MO2_VERSION,
        )
    except (OSError, ValueError) as exc:
        run.record("profile", "failed", str(exc))
        return create._finish(run, rep, paths, started, "add")
    run.record("profile", "ok", f"{len(report.get('layers') or [])} layer(s)")

    rep.stage("layer")
    new_folders = sorted(set(led.data["mods"]) - before)
    layer_sep = next(
        (
            entry.get("separators") or []
            for entry in report.get("layers") or []
            if entry.get("slug") == ctx.slug
        ),
        [],
    )
    rep.log(f"layer {len(led.data['layers'])}: {ctx.name} ({ctx.slug}@{ctx.revision})")
    rep.log(f"  mods:            {ctx.installed} ({len(new_folders)} new folder(s))")
    rep.log(f"  shared with other layers (not reinstalled): {len(ctx.shared)}")
    for folder in ctx.shared[:10]:
        rep.log(f"    {folder}  <- {', '.join(led.owners_of(folder))}")
    if len(ctx.shared) > 10:
        rep.log(f"    ... +{len(ctx.shared) - 10} more")
    rep.log(f"  separator(s):    {', '.join(layer_sep) or '(none)'}")
    rep.log(f"  user mods kept:  {len(report.get('user_mods') or [])}")
    if ctx.missing:
        rep.log(f"  NOT installed (unavailable on Nexus): {len(ctx.missing)}")
        for name in ctx.missing:
            rep.log(f"    {name}")
    ini_keys = led.ini_keys_of(ctx.owner)
    key_count = sum(len(keys) for sections in ini_keys.values() for keys in sections.values())
    rep.log(f"  INI keys set:    {key_count}")
    rep.done("layer", f"{ctx.slug}@{ctx.revision} added to {base.get('slug')} instance")
    run.record("layer", "ok", f"{ctx.installed} mods, {len(ctx.shared)} shared")

    return create._finish(run, rep, paths, started, "add")


# -- remove ----------------------------------------------------------------------------


def _layer_archives(paths: create.Paths, layer: dict[str, Any]) -> list[Path]:
    """The archives in `downloads/` this layer's `downloads.json` recorded."""
    downloads_json = profile.instance_path(paths.out, (layer.get("files") or {}).get("downloads"))
    if downloads_json is None or not downloads_json.exists():
        return []
    try:
        data = json.loads(downloads_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Path] = []
    for entry in data.get("entries") or []:
        path = entry.get("path")
        if path:
            out.append(Path(path))
    return out


def _layer_executables(
    paths: create.Paths, layer: dict[str, Any], game_path: str
) -> list[dict[str, str]]:
    """The `[customExecutables]` blocks this layer's manifest would have registered."""
    manifest_path = profile.instance_path(paths.out, layer.get("manifest"))
    install_path = profile.instance_path(paths.out, (layer.get("files") or {}).get("install"))
    if manifest_path is None or not manifest_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    if install_path is not None and install_path.exists():
        try:
            entries = json.loads(install_path.read_text(encoding="utf-8")).get("entries") or []
        except (OSError, ValueError):
            entries = []
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError):
        return []
    return profile.build_custom_executables(entries, manifest.get("tools") or [], game_path)


def _exe_key(block: dict[str, str]) -> tuple[str, str]:
    return (block.get("binary", "").strip().lower(), block.get("arguments", "").strip())


_EXE_LINE = re.compile(r"^(\d+)\\(\w+)=(.*)$")


def drop_executables(ini_path: Path, keys: set[tuple[str, str]]) -> list[str]:
    """Remove `[customExecutables]` entries matching `(binary, arguments)`; renumber the rest.

    The counterpart of `tools.merge_executables`, and line-based for the same reason (see
    that module's docstring: configparser mangles MO2's `@ByteArray(...)` values). Without
    this, removing a layer would leave MO2 showing shortcuts to patchers whose mod folder
    has just been deleted. Returns the titles removed.
    """
    if not keys or not ini_path.exists():
        return []
    lines = ini_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_ce = False
    blocks: dict[int, dict[str, str]] = {}
    order: list[int] = []
    size_line_idx: int | None = None
    tail: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_ce = stripped[1:-1] == "customExecutables"
            out.append(line)
            continue
        if in_ce:
            if stripped.startswith("size="):
                size_line_idx = len(out)
                out.append(line)
                continue
            m = _EXE_LINE.match(line)
            if m:
                idx = int(m.group(1))
                if idx not in blocks:
                    blocks[idx] = {}
                    order.append(idx)
                blocks[idx][m.group(2)] = m.group(3)
                continue
            if not stripped:
                continue  # blank lines inside the section are rebuilt below
            tail.append(line)
            continue
        out.append(line)

    kept = [i for i in order if _exe_key(blocks[i]) not in keys]
    removed = [blocks[i].get("title", "") for i in order if i not in kept]
    if not removed:
        return []

    rendered: list[str] = []
    for new_idx, old_idx in enumerate(kept, start=1):
        for key, value in blocks[old_idx].items():
            rendered.append(f"{new_idx}\\{key}={value}")
    rendered.extend(tail)
    rendered.append("")

    if size_line_idx is not None:
        out[size_line_idx] = f"size={len(kept)}"
        out[size_line_idx + 1 : size_line_idx + 1] = rendered
    else:
        out.extend(rendered)
    profile._write_keeping_newlines(ini_path, "\n".join(out) + "\n")
    return removed


def cmd_remove(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    started = time.monotonic()
    paths = _instance_paths(args.instance)
    if not (paths.out / ledger.LEDGER_NAME).exists():
        rep.warn(f"{paths.out} is not a c2wj instance ({ledger.LEDGER_NAME} not found)")
        return 2

    led = ledger.load(paths.out)
    create.migrate_instance(paths, led, rep)
    layers = led.data.get("layers") or []
    layer = led.layer_by_slug(args.slug)
    if layer is None:
        known = ", ".join(f"{entry.get('slug')}@{entry.get('revision')}" for entry in layers)
        rep.warn(f"{args.slug} is not a layer of this instance (layers: {known or 'none'})")
        return 2

    is_base = layers[0] is layer
    if is_base and not args.force:
        rep.warn(
            f"{args.slug} is the base layer this instance was created from: every other layer "
            "was installed on the assumption that it is present, the profile name and the "
            "game/Stock Game setup come from it, and removing it would leave an MO2 instance "
            "with no load order to speak of. Delete the whole instance folder instead, or pass "
            "--force if you really mean to strip it and keep the shell."
        )
        return 2

    owner = led.layer_owner(layer)
    rep.stage("remove")
    rep.log(f"removing layer {layer.get('name') or args.slug} ({owner})")

    # -- mod folders: only the ones nobody else owns ---------------------------------
    owned = led.mods_owned_by(owner)
    solely = set(led.mods_owned_only_by(owner))
    deleted: list[str] = []
    kept_shared: list[str] = []
    for folder in owned:
        target = paths.mods / folder
        if folder in solely:
            _rmtree(target)
            led.remove_mod_owner(folder, owner)
            deleted.append(folder)
        else:
            remaining = led.remove_mod_owner(folder, owner)
            kept_shared.append(folder)
            if target.is_dir() and remaining:
                installer.stamp_owner(target, ", ".join(remaining))

    # -- separators the layer created -------------------------------------------------
    other_separators = {
        name
        for entry in layers
        if entry is not layer
        for name in (entry.get("separators") or [])
    }
    separators_removed: list[str] = []
    for name in layer.get("separators") or []:
        if name in other_separators:
            continue
        _rmtree(paths.mods / name)
        separators_removed.append(name)

    # -- INI keys: back to what they were before this layer ---------------------------
    profile_name = layer.get("profile") or (layers[0].get("profile") if layers else "")
    profile_dir = paths.out / "profiles" / (profile_name or "")
    ini_notes = profile.revert_ini_keys(profile_dir, led.ini_keys_of(owner))
    for note in ini_notes:
        rep.log(note)
    led.drop_ini_keys(owner)

    # -- custom executables this layer registered --------------------------------------
    game = led.data.get("game") or {}
    exe_game_path = game.get("stock_game_dir") or game.get("source_path") or ""
    mine = {_exe_key(b) for b in _layer_executables(paths, layer, exe_game_path)}
    for other in layers:
        if other is layer:
            continue
        mine -= {_exe_key(b) for b in _layer_executables(paths, other, exe_game_path)}
    exes_removed = drop_executables(paths.out / "ModOrganizer.ini", mine)
    for title in exes_removed:
        rep.log(f"executable removed from ModOrganizer.ini: {title}")

    # -- stage JSON, manifest and (optionally) archives --------------------------------
    archives = _layer_archives(paths, layer) if args.purge_downloads else []
    stage_files: list[str] = []
    for rel in (layer.get("files") or {}).values():
        path = profile.instance_path(paths.out, rel)
        if path is not None and path.exists():
            path.unlink()
            stage_files.append(path.name)

    purged = 0
    if args.purge_downloads:
        keep: set[str] = set()
        for entry in layers:
            if entry is layer:
                continue
            keep.update(p.name.lower() for p in _layer_archives(paths, entry))
        for archive in archives:
            if archive.name.lower() in keep or not archive.exists():
                continue
            archive.unlink()
            meta = archive.with_name(archive.name + ".meta")
            if meta.exists():
                meta.unlink()
            purged += 1

    led.drop_layer(layer.get("slug") or "", layer.get("revision"))
    led.save()

    # -- re-render the profile from what is left ---------------------------------------
    rendered = None
    if led.data.get("layers"):
        try:
            rendered = create.render_profile(
                paths,
                led,
                rep,
                game_path=game.get("stock_game_dir") or game.get("source_path") or "",
                mo2_version=(led.data.get("mo2") or {}).get("version") or build.DEFAULT_MO2_VERSION,
            )
        except (OSError, ValueError) as exc:
            rep.warn(f"profile could not be re-rendered: {exc}")
    else:
        rep.warn("no layers left: the profile was not re-rendered (modlist.txt left as it was)")

    rep.stage("summary")
    rep.log(f"  layer removed:      {args.slug}@{layer.get('revision')}")
    rep.log(f"  mod folders deleted:{len(deleted):>4}")
    rep.log(f"  shared, kept:       {len(kept_shared):>4} (owner dropped from each)")
    for folder in kept_shared[:10]:
        rep.log(f"    {folder}  -> {', '.join(led.owners_of(folder))}")
    if len(kept_shared) > 10:
        rep.log(f"    ... +{len(kept_shared) - 10} more")
    rep.log(f"  separators removed: {len(separators_removed):>4}")
    rep.log(f"  executables removed:{len(exes_removed):>4}")
    rep.log(f"  INI files reverted: {len(ini_notes):>4}")
    rep.log(f"  stage files removed:{len(stage_files):>4} ({', '.join(stage_files) or 'none'})")
    if args.purge_downloads:
        rep.log(f"  archives purged:    {purged:>4}")
    else:
        rep.log("  archives kept in downloads/ (pass --purge-downloads to delete them)")
    if rendered:
        rep.log(f"  layers left:        {len(rendered.get('layers') or [])}")
        rep.log(f"  modlist entries:    {len(rendered.get('mod_order') or [])}")
        rep.log(f"  user mods kept:     {len(rendered.get('user_mods') or [])}")
    rep.log(f"  elapsed: {time.monotonic() - started:.1f}s")
    rep.done("remove", f"{args.slug} removed; {len(deleted)} folder(s) deleted")
    return 0


# -- parsers ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "add",
        help="layer another collection on top of an existing c2wj instance",
    )
    p.add_argument("url", help="collection URL of the add-on collection")
    p.add_argument("--instance", required=True, help="the instance directory to add it to")
    p.add_argument("--revision", type=int, default=None, help="revision number (default: latest)")
    p.add_argument("--jobs", type=int, default=4, help="parallel workers per stage (default: 4)")
    p.add_argument(
        "--game-path",
        default=None,
        help="the game install to install against (default: the instance ledger's)",
    )
    p.add_argument(
        "--choices-overrides",
        default=None,
        help='JSON file of {"<tag>": <Vortex choices object>} for fresh-mode FOMODs',
    )
    p.add_argument(
        "--skip-survey",
        action="store_true",
        default=False,
        help="skip the Nexus content-preview survey (it costs hourly API budget)",
    )
    p.add_argument(
        "--allow-missing",
        action="store_true",
        default=False,
        help="carry on when Nexus no longer serves a file the collection pinned (the "
        "author deleted it); those mods are left out and listed in the summary. An "
        "md5 mismatch still stops the run.",
    )
    p.add_argument(
        "--reuse-downloads",
        default=None,
        help="an existing download store to hardlink/copy archives + .meta from first",
    )
    p.set_defaults(func=cmd_add)

    r = subparsers.add_parser(
        "remove",
        help="remove a collection layer from an instance (keeping shared and user mods)",
    )
    r.add_argument("slug", help="the collection slug to remove, e.g. xk05aw")
    r.add_argument("--instance", required=True, help="the instance directory")
    r.add_argument(
        "--purge-downloads",
        action="store_true",
        default=False,
        help="also delete the layer's archives from downloads/ (they are kept by default: "
        "harmless, and useful if you re-add the layer)",
    )
    r.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="allow removing the base layer the instance was created from",
    )
    r.set_defaults(func=cmd_remove)
