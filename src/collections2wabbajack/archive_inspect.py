"""Inspect downloaded mod archives: layout, FOMOD installers, plugins, BSAs.

Consumes downloads.json (written by the `download` command) and, for every
archive that downloaded successfully, lists its contents via sevenzip.py and
classifies its on-disk layout. The headline question this answers: of the
`fresh`-install mods, how many ship a FOMOD installer (fomod/ModuleConfig.xml)
that will need special handling later.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .reporter import Reporter, get_reporter
from .sevenzip import ArchiveEntry, list_archive

MANIFEST_ENTRY_FIELDS = (
    "name",
    "mod_id",
    "file_id",
    "tag",
    "md5",
    "install_mode",
    "optional",
    "phase",
    "mod_type",
    "file_name",
    "path",
)

_DATA_DIR_NAMES = {
    "meshes",
    "textures",
    "scripts",
    "interface",
    "skse",
    "sound",
    "strings",
    "seq",
    "shadersfx",
}
_DATA_FILE_EXTS = {".esp", ".esm", ".esl", ".bsa", ".ini"}
_PLUGIN_EXTS = {".esp", ".esm", ".esl"}
_ROOT_EXTS = {".exe", ".dll"}


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""


def _find_fomod_dir(entries: list[ArchiveEntry]) -> str | None:
    for e in entries:
        if e.is_dir:
            continue
        parts = e.path.split("/")
        if (
            len(parts) >= 2
            and parts[-1].lower() == "moduleconfig.xml"
            and parts[-2].lower() == "fomod"
        ):
            return "/".join(parts[:-1])
    return None


def _top_level_names(entries: list[ArchiveEntry]) -> list[str]:
    names = {e.path.split("/", 1)[0] for e in entries if e.path}
    return sorted(names)


def _is_top_folder(name: str, entries: list[ArchiveEntry]) -> bool:
    prefix = name + "/"
    for e in entries:
        if e.path == name and e.is_dir:
            return True
        if e.path.startswith(prefix):
            return True
    return False


def _classify_layout(
    top_level: list[str], entries: list[ArchiveEntry], fomod_dir: str | None
) -> str:
    fomod_top = fomod_dir.split("/")[0].lower() if fomod_dir else None
    considered = [t for t in top_level if fomod_top is None or t.lower() != fomod_top]

    def has_data_content(names: list[str]) -> bool:
        for n in names:
            if n.lower() in _DATA_DIR_NAMES:
                return True
            if _ext(n) in _DATA_FILE_EXTS:
                return True
        return False

    if has_data_content(considered):
        return "data"
    if (
        len(considered) == 1
        and considered[0].lower() == "data"
        and _is_top_folder(considered[0], entries)
    ):
        return "data_wrapped"
    if (
        len(considered) == 1
        and considered[0].lower() != "data"
        and _is_top_folder(considered[0], entries)
    ):
        return "single_folder"
    if any(_ext(n) in _ROOT_EXTS for n in considered):
        return "root"
    if fomod_dir is not None:
        return "fomod_only"
    return "unknown"


def inspect_archive(path: Path | str) -> dict[str, Any]:
    """List and classify one archive. Raises on listing failure (see sevenzip.list_archive)."""
    path = Path(path)
    entries = list_archive(path)
    files = [e for e in entries if not e.is_dir]

    fomod_dir = _find_fomod_dir(entries)
    top_level = _top_level_names(entries)
    layout = _classify_layout(top_level, entries, fomod_dir)

    plugins = sorted({e.path.rsplit("/", 1)[-1] for e in files if _ext(e.path) in _PLUGIN_EXTS})
    bsa_count = sum(1 for e in files if _ext(e.path) == ".bsa")

    has_skse_plugin = False
    for e in files:
        parts = e.path.lower().split("/")
        if (
            len(parts) >= 3
            and parts[-1].endswith(".dll")
            and parts[-2] == "plugins"
            and parts[-3] == "skse"
        ):
            has_skse_plugin = True
            break

    has_dll_root = any("/" not in e.path and _ext(e.path) in _ROOT_EXTS for e in files)

    return {
        "archive_type": path.suffix.lower().lstrip("."),
        "file_count": len(files),
        "total_size": sum(e.size for e in files),
        "has_fomod": fomod_dir is not None,
        "fomod_dir": fomod_dir,
        "top_level": top_level,
        "layout": layout,
        "plugins": plugins,
        "bsa_count": bsa_count,
        "has_skse_plugin": has_skse_plugin,
        "has_dll_root": has_dll_root,
    }


def _inspect_one(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    base = {k: entry.get(k) for k in MANIFEST_ENTRY_FIELDS}
    try:
        return base, inspect_archive(entry["path"]), None
    except Exception as exc:  # noqa: BLE001 - surfaced via report / return code
        return base, None, str(exc)


def _print_report(entries: list[dict[str, Any]], failures: list[tuple[str, str]]) -> None:
    archive_types = Counter(e["archive_type"] for e in entries)
    layouts = Counter(e["layout"] for e in entries)
    mode_fomod = Counter((e["install_mode"], e["has_fomod"]) for e in entries)

    print("\n== archive types")
    for k, v in sorted(archive_types.items()):
        print(f"  {k or '(none)'}: {v}")

    print("\n== layouts")
    for k, v in sorted(layouts.items()):
        print(f"  {k}: {v}")

    print("\n== install_mode x has_fomod")
    for (mode, has_fomod), v in sorted(mode_fomod.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        print(f"  {mode} / fomod={has_fomod}: {v}")

    fresh_fomod = [e for e in entries if e["install_mode"] == "fresh" and e["has_fomod"]]
    print(f"\n== fresh mods WITH a FOMOD ({len(fresh_fomod)})")
    for e in fresh_fomod:
        print(f"  - {e['name']}")

    choices_no_fomod = [e for e in entries if e["install_mode"] == "choices" and not e["has_fomod"]]
    print(f"\n== choices mods WITHOUT a FOMOD -- anomalies ({len(choices_no_fomod)})")
    for e in choices_no_fomod:
        print(f"  - {e['name']}")

    dinput = [e for e in entries if e["mod_type"] == "dinput"]
    print(f"\n== mod_type == 'dinput' ({len(dinput)})")
    for e in dinput:
        print(f"  - {e['name']}: layout={e['layout']}")

    with_plugins = [e for e in entries if e["plugins"]]
    total_plugins = sum(len(e["plugins"]) for e in entries)
    total_bsas = sum(e["bsa_count"] for e in entries)
    print(
        f"\n== plugins: {len(with_plugins)} mods ship plugins, "
        f"{total_plugins} plugins total, {total_bsas} BSAs total"
    )

    if failures:
        print(f"\n== FAILED to inspect ({len(failures)})")
        for name, err in failures:
            print(f"  - {name}: {err}")


def cmd_inspect(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    downloads_json = Path(args.downloads_json).resolve()
    data = json.loads(downloads_json.read_text(encoding="utf-8"))
    candidates = [e for e in data.get("entries", []) if e.get("status") in ("ok", "skipped")]

    rep.stage("inspect", len(candidates))
    results: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(_inspect_one, e): e for e in candidates}
        for fut in as_completed(futures):
            base, inspection, error = fut.result()
            done += 1
            name = base.get("name") or base.get("file_name") or "?"
            rep.progress(done, len(candidates), f"{'FAILED  ' if error else 'ok      '} {name}")
            if error is not None:
                failures.append((name, error))
                continue
            results.append({**base, **inspection})

    results.sort(key=lambda e: (e.get("name") or ""))

    out_path = Path(args.out) if args.out else downloads_json.parent / "inspect.json"
    out_path.write_text(
        json.dumps({"downloads_json": str(downloads_json), "entries": results}, indent=2),
        encoding="utf-8",
    )
    rep.done(
        "inspect",
        f"{len(results)} archives inspected, {len(failures)} failed -> {out_path}",
    )

    _print_report(results, failures)
    return 1 if failures else 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "inspect", help="inspect downloaded archives (layout, FOMOD, plugins)"
    )
    p.add_argument("downloads_json", help="path to downloads.json")
    p.add_argument("--out", default=None, help="output path (default: <same dir>/inspect.json)")
    p.add_argument("--jobs", type=int, default=4, help="parallel archive listings (default: 4)")
    p.set_defaults(func=cmd_inspect)
