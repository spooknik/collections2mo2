"""Build an MO2 ``mods/`` tree from the downloaded archives.

For every archive recorded in ``inspect.json`` we pick exactly one install
strategy and record which one we used:

``replicate``
    The manifest lists the mod's exact file set (``mod["hashes"]``). Extract and
    copy those files, verifying each MD5.
``fomod-choices`` / ``fomod-overrides`` / ``fomod-defaults``
    The archive ships a FOMOD installer. Replay the curator's recorded wizard
    answers, answers supplied via ``--choices-overrides``, or the defaults.
``data`` / ``data_wrapped`` / ``single_folder`` / ``root`` / ``as_is`` / ``override_json``
    A plain archive, normalised by :mod:`.layout`.
``existing``
    The mod folder was already there and ``--force`` was not given.

The result is ``<mods-dir>/../install.json``, which is the contract the profile
writer consumes: one entry per mod, in manifest order, carrying the folder name,
the strategy, the plugins that landed at the mod root, how many of the FOMOD's
``fileDependency`` checks we could actually answer, and any warnings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fomod, layout
from .naming import assign_folder_names, mod_folder_name
from .sevenzip import extract

PLUGIN_EXTS = {".esp", ".esm", ".esl"}
# What a FOMOD <fileDependency> can plausibly name, and what we therefore
# remember from the mods we have already installed in this run.
GAME_FILE_EXTS = PLUGIN_EXTS | {".bsa", ".ba2", ".dll", ".exe"}
MAX_WARNINGS_SHOWN = 8


@dataclass
class ModResult:
    """One row of install.json, plus a `failed` flag that decides the exit code."""

    name: str
    folder: str
    tag: str
    md5: str
    mod_id: Any
    file_id: Any
    phase: Any
    optional: bool
    install_mode: str
    mod_type: str
    strategy: str = "?"
    file_count: int = 0
    root_file_count: int = 0
    plugins: list[str] = field(default_factory=list)
    # How much of this FOMOD's <fileDependency> questions we could actually answer.
    fomod_resolved_deps: int = 0
    fomod_unknown_deps: int = 0
    warnings: list[str] = field(default_factory=list)
    failed: bool = False

    def as_json(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "failed"}
        return data


# --------------------------------------------------------------- file dependencies


class FileStateIndex:
    """Answers a FOMOD ``fileDependency`` -- ``(filename) -> Active/Inactive/Missing``.

    A FOMOD asks about game files ("is ccbgssse002-exoticarrows.esl active?").
    Assuming ``Missing`` makes every Creation Club / DLC patch resolve to
    ``NotUsable``, so we answer from what we actually know, in this order:

    1. the manifest's top-level ``plugins`` list -- the curator's real load
       order, which is the closest thing we have to the machine the choices
       were recorded on;
    2. the game's own ``Data`` folder, when ``--game-path`` is given -- this is
       where the base game masters and the Creation Club content live;
    3. files earlier mods in *this* run already installed.

    Everything else stays ``Missing``, which also means "we have no idea";
    :mod:`.fomod` counts those separately so install.json can show the coverage.

    Installs run in parallel, so (3) is best-effort: a mod finishing at the same
    time as this lookup may not be visible yet. That is deliberate -- (1) covers
    the common case and serialising the whole run to fix it is not worth it.
    """

    def __init__(self, plugins: Iterable[dict[str, Any]] | None, game_path: Path | None) -> None:
        self._known: dict[str, str] = {}
        for entry in plugins or ():
            if not isinstance(entry, dict):
                continue
            name = _basename(str(entry.get("name") or ""))
            if name:
                self._known.setdefault(name, "Active" if entry.get("enabled") else "Inactive")
        self.manifest_count = len(self._known)
        # The manifest wins where the two disagree: it is the load order the
        # choices were recorded against, and it knows about "installed but off".
        before = len(self._known)
        for name in _scan_game_data(game_path):
            self._known.setdefault(name, "Active")
        self.game_count = len(self._known) - before
        self._lock = threading.Lock()
        self._installed: set[str] = set()

    def add_installed(self, names: Iterable[str]) -> None:
        """Record file basenames a mod just installed, for the mods still to come."""
        batch = {n for n in (_basename(x) for x in names) if n}
        if batch:
            with self._lock:
                self._installed |= batch

    def __call__(self, filename: str) -> str:
        name = _basename(filename)
        state = self._known.get(name)
        if state is not None:
            return state
        with self._lock:
            if name in self._installed:
                return "Active"
        return "Missing"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _scan_game_data(game_path: Path | None) -> list[str]:
    """Plugin and archive basenames in the game's ``Data`` folder (scanned once)."""
    if game_path is None:
        return []
    data = game_path / "Data"
    if not data.is_dir():
        # Tolerate being handed the Data folder itself.
        data = game_path
    if not data.is_dir():
        return []
    return [
        p.name.lower()
        for p in data.iterdir()
        if p.is_file() and p.suffix.lower() in (PLUGIN_EXTS | {".bsa", ".ba2"})
    ]


# ------------------------------------------------------------------ file plumbing


def _walk(base: Path) -> list[str]:
    """Every file under `base`, as '/'-separated relative paths."""
    return [
        p.relative_to(base).as_posix()
        for p in base.rglob("*")
        if p.is_file()
    ]


def _index(base: Path) -> dict[str, str]:
    """Lowercased relative path -> real relative path, for files *and* directories."""
    index: dict[str, str] = {}
    for path in base.rglob("*"):
        rel = path.relative_to(base).as_posix()
        index.setdefault(rel.lower(), rel)
    return index


def _copy_pairs(
    base: Path,
    pairs: list[tuple[str, str]],
    dest_root: Path,
    warnings: list[str],
    landed: list[str] | None = None,
) -> int:
    """Copy `(source, destination)` pairs from `base` into `dest_root`.

    Sources are resolved case-insensitively (archives are inconsistent about
    casing and FOMOD authors are worse). A source that resolves to a directory
    copies its whole tree under the destination; later pairs overwrite earlier
    ones, which is how FOMOD priority ordering is meant to work.

    `landed` collects the basenames of the game files we wrote, so later mods in
    the run can answer ``fileDependency`` questions about them.
    """
    index = _index(base)
    copied = 0

    def note(target: Path) -> None:
        if landed is not None and target.suffix.lower() in GAME_FILE_EXTS:
            landed.append(target.name)

    for source, destination in pairs:
        actual = index.get(source.strip("/").lower())
        if actual is None:
            warnings.append(f"source not found in archive: {source}")
            continue
        src = base / actual
        if src.is_dir():
            for child in sorted(src.rglob("*")):
                if not child.is_file():
                    continue
                rel = child.relative_to(src).as_posix()
                target = dest_root / destination / rel if destination else dest_root / rel
                copied += _copy_file(child, target)
                note(target)
        else:
            target = dest_root / destination if destination else dest_root / src.name
            copied += _copy_file(src, target)
            note(target)
    return copied


def _clear_readonly(func, path: str, _exc) -> None:
    """rmtree hook: some archives ship read-only dirs, which Windows won't rmdir."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """Delete a tree, clearing read-only bits and retrying a held-open handle."""
    for attempt in range(3):
        if not path.exists():
            return
        shutil.rmtree(path, onexc=_clear_readonly)
        if not path.exists():
            return
        time.sleep(0.2 * (attempt + 1))


def _copy_file(src: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return 1


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------- strategies


def _plan_replicate(
    mod: dict[str, Any], base: Path, warnings: list[str]
) -> list[tuple[str, str]]:
    """`hashes` lists the mod's exact file set, relative to the mod root."""
    if mod.get("patches"):
        warnings.append("patches not implemented")
    index = _index(base)
    pairs: list[tuple[str, str]] = []
    for item in mod.get("hashes") or []:
        rel = str(item.get("path") or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        actual = index.get(rel.lower())
        if actual is None:
            warnings.append(f"replicate: file listed in manifest is missing: {rel}")
            continue
        expected = (item.get("md5") or "").lower()
        if expected:
            got = _md5(base / actual)
            if got != expected:
                warnings.append(f"replicate: MD5 mismatch for {rel} ({got} != {expected})")
        pairs.append((actual, rel))
    return pairs


def _find_module_config(base: Path) -> Path | None:
    hits = [p for p in base.rglob("*") if p.is_file() and p.name.lower() == "moduleconfig.xml"]
    if not hits:
        return None
    return min(hits, key=lambda p: len(p.relative_to(base).parts))


def _plan_fomod(
    base: Path,
    choices: dict[str, Any] | None,
    warnings: list[str],
    file_state: fomod.FileState | None,
) -> tuple[Path, fomod.InstallPlan] | None:
    config = _find_module_config(base)
    if config is None:
        return None
    # The FOMOD root is the directory holding the `fomod` folder; every <file>
    # and <folder> source in ModuleConfig.xml is relative to it.
    root = config.parent.parent
    try:
        plan = fomod.evaluate(config, _walk(root), choices, file_state)
    except Exception as exc:  # noqa: BLE001 - one bad installer must not stop the run
        warnings.append(f"FOMOD evaluation failed ({exc}); falling back to layout rules")
        return None
    warnings.extend(plan.warnings)
    return root, plan


# ------------------------------------------------------------------------ one mod


def _install_one(
    entry: dict[str, Any],
    mod: dict[str, Any],
    mods_dir: Path,
    tmp_root: Path,
    game_name: str,
    overrides: dict[str, Any],
    force: bool,
    file_state: FileStateIndex | None = None,
    folder: str | None = None,
) -> ModResult:
    tag = entry.get("tag") or ""
    result = ModResult(
        name=mod.get("name") or entry.get("name") or "",
        folder=folder or (mod_folder_name(mod) if mod else (entry.get("name") or "")),
        tag=tag,
        md5=entry.get("md5") or "",
        mod_id=entry.get("mod_id"),
        file_id=entry.get("file_id"),
        phase=mod.get("phase", entry.get("phase")),
        optional=bool(entry.get("optional")),
        install_mode=entry.get("install_mode") or "fresh",
        mod_type=entry.get("mod_type") or "",
    )
    dest_root = mods_dir / result.folder
    if dest_root.exists() and not force:
        result.strategy = "existing"
        result.file_count = sum(1 for p in dest_root.rglob("*") if p.is_file())
        result.plugins = _root_plugins(dest_root)
        result.root_file_count = _root_folder_count(dest_root)
        if file_state is not None:
            file_state.add_installed(result.plugins)
        return result

    tmp = tmp_root / (tag or result.folder)
    _rmtree(tmp)
    try:
        extract(entry["path"], tmp)
        if dest_root.exists():
            _rmtree(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)

        base = tmp
        if result.install_mode == "replicate" or mod.get("hashes"):
            result.strategy = "replicate"
            pairs = _plan_replicate(mod, base, result.warnings)
        else:
            fomod_plan = None
            if entry.get("has_fomod"):
                choices = mod.get("choices")
                if choices:
                    strategy = "fomod-choices"
                elif tag in overrides:
                    choices, strategy = overrides[tag], "fomod-overrides"
                else:
                    choices, strategy = None, "fomod-defaults"
                fomod_plan = _plan_fomod(base, choices, result.warnings, file_state)
                if fomod_plan is not None:
                    result.strategy = strategy
                    base, plan = fomod_plan
                    pairs = plan.files
                    result.fomod_resolved_deps = plan.resolved_deps
                    result.fomod_unknown_deps = plan.unknown_deps
            if fomod_plan is None:
                plan = layout.plan_layout(
                    _walk(base),
                    result.mod_type,
                    read_text=lambda rel: (base / rel).read_text(encoding="utf-8-sig", errors="replace"),
                )
                result.strategy = plan.strategy
                result.warnings.extend(plan.warnings)
                pairs = plan.files

        landed: list[str] = []
        result.file_count = _copy_pairs(base, pairs, dest_root, result.warnings, landed)
        result.plugins = _root_plugins(dest_root)
        result.root_file_count = _root_folder_count(dest_root)
        _write_meta_ini(dest_root, entry, mod, game_name)
        if file_state is not None:
            # Publish before returning: mods still queued can now see these files.
            file_state.add_installed(landed)
    except Exception as exc:  # noqa: BLE001 - reported per mod, run continues
        result.failed = True
        result.strategy = "failed"
        result.warnings.append(f"install failed: {exc}")
    finally:
        _rmtree(tmp)
    return result


def _root_plugins(dest_root: Path) -> list[str]:
    return sorted(
        p.name
        for p in dest_root.glob("*")
        if p.is_file() and p.suffix.lower() in PLUGIN_EXTS
    )


def _root_folder_count(dest_root: Path) -> int:
    """Files under the mod's ``Root/`` folder (Root Builder / game-folder content)."""
    root = next((p for p in dest_root.glob("*") if p.is_dir() and p.name.lower() == "root"), None)
    return sum(1 for p in root.rglob("*") if p.is_file()) if root else 0


def _ini_value(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _write_meta_ini(
    dest_root: Path, entry: dict[str, Any], mod: dict[str, Any], game_name: str
) -> None:
    lines = [
        "[General]",
        f"modid={entry.get('mod_id') or 0}",
        f"version={_ini_value(mod.get('version'))}",
        "newestVersion=",
        "category=0",
        f"installationFile={_ini_value(entry.get('file_name'))}",
        "repository=Nexus",
        f"gameName={game_name}",
        "comments=",
        f"notes={_ini_value(mod.get('instructions'))}",
        "nexusFileStatus=1",
        "hasCustomURL=false",
        "validated=true",
        "converted=false",
        "",
        "[installedFiles]",
        f"1\\modid={entry.get('mod_id') or 0}",
        f"1\\fileid={entry.get('file_id') or 0}",
        "size=1",
        "",
    ]
    (dest_root / "meta.ini").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------- reporting


def _report(results: list[ModResult]) -> None:
    strategies = Counter(r.strategy for r in results)
    total_files = sum(r.file_count for r in results)
    warned = [r for r in results if r.warnings]

    print("\n== strategies")
    for name, count in sorted(strategies.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name}: {count}")
    print(f"\n{len(results)} mods, {total_files} files installed, {len(warned)} with warnings")

    if warned:
        print(f"\n== warnings ({len(warned)} mods)")
        for r in warned:
            print(f"  - {r.name} [{r.strategy}]")
            for warning in r.warnings[:MAX_WARNINGS_SHOWN]:
                print(f"      {warning}")
            if len(r.warnings) > MAX_WARNINGS_SHOWN:
                print(f"      ... +{len(r.warnings) - MAX_WARNINGS_SHOWN} more")

    failed = [r for r in results if r.failed]
    if failed:
        print(f"\n== FAILED ({len(failed)})")
        for r in failed:
            print(f"  - {r.name}: {r.warnings[-1] if r.warnings else 'unknown error'}")


# ------------------------------------------------------------------------ command


def _default_mods_dir(inspect_json: Path) -> Path:
    # <...>/<rev>/downloads/inspect.json -> <...>/<rev>/mo2/mods
    return inspect_json.parent.parent / "mo2" / "mods"


def cmd_install(args: argparse.Namespace) -> int:
    inspect_json = Path(args.inspect_json).resolve()
    inspect_data = json.loads(inspect_json.read_text(encoding="utf-8"))
    entries = inspect_data.get("entries", [])

    downloads_json = Path(inspect_data.get("downloads_json") or inspect_json.parent / "downloads.json")
    downloads: dict[str, Any] = {}
    if downloads_json.exists():
        downloads = json.loads(downloads_json.read_text(encoding="utf-8"))
    game_name = downloads.get("game_name") or ""

    manifest_path = Path(args.manifest) if args.manifest else Path(downloads.get("manifest", ""))
    if not manifest_path or not manifest_path.exists():
        print(f"error: manifest not found ({manifest_path})", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mods = manifest.get("mods", [])
    by_tag = {(m.get("source") or {}).get("tag"): m for m in mods}
    folder_names = assign_folder_names(mods)
    order = {(m.get("source") or {}).get("tag"): i for i, m in enumerate(mods)}

    overrides: dict[str, Any] = {}
    if args.choices_overrides:
        overrides = json.loads(Path(args.choices_overrides).read_text(encoding="utf-8"))

    game_path = Path(args.game_path).resolve() if args.game_path else None
    if game_path is not None and not game_path.is_dir():
        print(f"warning: --game-path {game_path} is not a directory, ignored", file=sys.stderr)
        game_path = None
    file_state = FileStateIndex(manifest.get("plugins"), game_path)
    print(
        f"fileDependency resolver: {file_state.manifest_count} plugins from the manifest, "
        f"{file_state.game_count} more from the game folder"
    )

    mods_dir = Path(args.mods_dir).resolve() if args.mods_dir else _default_mods_dir(inspect_json)
    mods_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = mods_dir.parent / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    todo = [e for e in entries if e.get("tag") in by_tag]
    missing = [e for e in entries if e.get("tag") not in by_tag]
    for entry in missing:
        print(f"warning: {entry.get('name')} has no manifest entry (tag {entry.get('tag')}), skipped")
    if args.only:
        needle = args.only.lower()
        todo = [e for e in todo if needle in (e.get("name") or "").lower()]
    todo.sort(key=lambda e: order.get(e.get("tag"), 0))

    results: list[ModResult] = []
    total = len(todo)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(
                _install_one,
                entry,
                by_tag[entry["tag"]],
                mods_dir,
                tmp_root,
                game_name,
                overrides,
                args.force,
                file_state,
                folder_names.get(entry["tag"]),
            ): entry
            for entry in todo
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            done += 1
            flag = "!" if result.warnings else " "
            print(
                f"[{done}/{total}] {result.strategy:<16} {flag} {result.name}  "
                f"({result.file_count} files)"
            )

    results.sort(key=lambda r: order.get(r.tag, 0))
    _rmtree(tmp_root)

    out_path = mods_dir.parent / "install.json"
    # A re-run that skips existing folders must not erase what the original install
    # recorded (strategy, warnings such as default FOMOD picks); carry those forward.
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8")).get("entries", [])
        except (OSError, ValueError):
            previous = []
        prev_by_tag = {p.get("tag"): p for p in previous if p.get("strategy") != "existing"}
        for r in results:
            prev = prev_by_tag.get(r.tag)
            if r.strategy == "existing" and prev and prev.get("folder") == r.folder:
                r.strategy = prev.get("strategy") or r.strategy
                r.warnings = list(prev.get("warnings") or [])
                r.fomod_resolved_deps = int(prev.get("fomod_resolved_deps") or 0)
                r.fomod_unknown_deps = int(prev.get("fomod_unknown_deps") or 0)
    else:
        previous = []

    # A filtered run (--only) only touched some mods; keep the previous entries for
    # the rest so install.json always describes the whole instance, in manifest order.
    entries_out: list[dict[str, Any]] = [r.as_json() for r in results]
    if args.only and previous:
        done_tags = {r.tag for r in results}
        entries_out.extend(p for p in previous if p.get("tag") not in done_tags)
        entries_out.sort(key=lambda p: order.get(p.get("tag"), 0))

    out_path.write_text(
        json.dumps(
            {
                "manifest": str(manifest_path.resolve()),
                "mods_dir": str(mods_dir),
                "game_name": game_name,
                "entries": entries_out,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out_path}")
    _report(results)
    return 1 if any(r.failed for r in results) else 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("install", help="build mods/ from the downloaded archives")
    p.add_argument("inspect_json", help="path to inspect.json")
    p.add_argument("--manifest", default=None, help="collection.json (default: from downloads.json)")
    p.add_argument(
        "--mods-dir", default=None, help="output mods dir (default: <rev>/mo2/mods)"
    )
    p.add_argument("--only", default=None, help="install only mods whose name contains this")
    p.add_argument(
        "--game-path",
        default=None,
        help="game install dir (its Data folder answers FOMOD fileDependency checks)",
    )
    p.add_argument("--jobs", type=int, default=4, help="parallel installs (default: 4)")
    p.add_argument("--force", action="store_true", help="reinstall mods that already exist")
    p.add_argument(
        "--choices-overrides",
        default=None,
        help='JSON file of {"<tag>": <Vortex choices object>} for fresh-mode FOMODs',
    )
    p.set_defaults(func=cmd_install)
