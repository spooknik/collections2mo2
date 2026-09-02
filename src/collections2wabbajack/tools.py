"""Download optional modding tools into a generated MO2 instance.

`c2wj tools list` prints the catalogue (`tools_catalog.json`, next to this module) with
installed/not-installed status. `c2wj tools install <id...> --mo2-dir <instance>` resolves
each catalogue entry's source (a GitHub release asset, a Nexus "main" file, or a direct
URL), downloads it into `<repo>/tools/cache/` (the same on-disk cache location build.py
uses for the MO2/Root Builder downloads, `<repo>/tools/cache/`, though this module does
not import build.py -- see below), extracts it with `sevenzip.extract`, optionally strips
one wrapping folder, and moves the result to `<mo2-dir>/Tools/<id>/`. It then:

- appends MO2 executable entries for the tool into `[customExecutables]` of
  `<mo2-dir>/ModOrganizer.ini`, line-based (matching how build.py's `_rewrite_ini` and
  profile.py's `render_mo2_ini`/`build_custom_executables` already read and write that
  file -- configparser mangles MO2's `@ByteArray(...)`-wrapped, backslash-doubled
  `gamePath` and the `N\\key=` custom-executable keys), deduping by binary path;
- copies the downloaded archive into `<mo2-dir>/downloads/` with an MO2/Wabbajack `.meta`
  sidecar (`directURL=` for GitHub/direct sources, `modID=`/`fileID=` for Nexus ones);
- installs any of the catalogue entry's `companion_mods` (a tool like DynDOLOD that
  needs its own mods present, e.g. DynDOLOD Resources SE / DLL NG) into
  `<mo2-dir>/mods/<Name>/` via `installer.install_single_mod` -- the same archive ->
  layout/FOMOD-default machinery `c2wj install` uses for a collection's own mods, just
  without a manifest behind it -- owned by `tool:<id>` in the ledger, and re-renders the
  profile (`profile.render_instance`) so they show up enabled in `modlist.txt`; and
- records the install in `<mo2-dir>/c2wj-instance.json` under `tools[id]` (via
  `ledger.py`, so companion mods land in the same `mods` map a collection's do).

`c2wj tools remove <id...> --mo2-dir <instance>` is the inverse: deletes `Tools/<id>`,
drops its `[customExecutables]` entries, removes its companion mods (only the folders it
solely owns -- shared with nothing else, since a companion mod is not something a
collection would also pin), drops the ledger records, and re-renders the profile.

Only the tool-archive download/extract/executable-registration path stays deliberately
self-contained from build.py, cli.py and profile.py (no imports of those, cyclic or
otherwise): it re-derives `<repo>/tools/cache/` from its own file location and
reimplements the small cached-download and single-root-flatten helpers rather than
importing build.py's private ones. `ledger.py`, `installer.py` and `profile.py` are now
stable, so companion-mod install/remove and the post-install profile re-render use them
directly (the `profile` import is function-local, matching the one `profile.py` already
takes back on this module, to keep the import graph acyclic either way round).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from . import installer as installer_mod
from . import ledger as ledger_mod
from .naming import sanitize_folder_name
from .nexus import USER_AGENT, AuthRequired, NexusClient, NexusError
from .sevenzip import extract

# Repo root is two levels above this file: src/collections2wabbajack/tools.py
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "tools" / "cache"
CATALOG_PATH = Path(__file__).resolve().parent / "tools_catalog.json"

GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
_CHUNK_SIZE = 1 << 20  # 1 MiB
LEDGER_NAME = ledger_mod.LEDGER_NAME


class ToolError(RuntimeError):
    pass


# -- Catalogue ------------------------------------------------------------------------


def load_catalog() -> list[dict[str, Any]]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return data["tools"]


def load_companion_catalog() -> list[dict[str, Any]]:
    """The top-level `companion_mods` list: mods a catalogue tool needs installed
    alongside it (e.g. DynDOLOD's Resources SE / DLL NG), addressed by id from a tool
    entry's own `companion_mods` (a list of ids into this list, not full records)."""
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return data.get("companion_mods") or []


def _catalog_by_id(catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in catalog}


def _is_disabled(entry: dict[str, Any]) -> bool:
    return bool((entry.get("install") or {}).get("disabled"))


# -- Downloading (self-contained cache, mirrors build.py's _download_cached) ----------


def _download_cached(url: str, dest: Path, *, headers: dict[str, str] | None = None) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  using cached {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    with requests.get(url, stream=True, headers=req_headers, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(tmp_dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(
                        f"\r    {dest.name}: {downloaded / 1e6:.1f}/{total / 1e6:.1f} MB "
                        f"({pct:.0f}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r    {dest.name}: {downloaded / 1e6:.1f} MB", end="", flush=True)
    print()
    tmp_dest.replace(dest)
    return dest


# -- Source resolution ------------------------------------------------------------------


class Resolved:
    def __init__(
        self,
        *,
        download_url: str,
        filename: str,
        version: str,
        source_kind: str,
        nexus_domain: str | None = None,
        nexus_mod_id: int | None = None,
        nexus_file_id: int | None = None,
    ):
        self.download_url = download_url
        self.filename = filename
        self.version = version
        self.source_kind = source_kind
        self.nexus_domain = nexus_domain
        self.nexus_mod_id = nexus_mod_id
        self.nexus_file_id = nexus_file_id


def _resolve_github(source: dict[str, Any]) -> Resolved:
    repo = source["repo"]
    tag = source.get("tag") or "latest"
    api_url = (
        f"{GITHUB_API}/repos/{repo}/releases/latest"
        if tag == "latest"
        else f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    )
    resp = requests.get(api_url, headers=_GITHUB_HEADERS, timeout=30)
    if resp.status_code == 404:
        raise ToolError(f"GitHub release not found: {repo}@{tag} ({api_url})")
    resp.raise_for_status()
    data = resp.json()
    version = data.get("tag_name") or tag
    assets = data.get("assets") or []
    pattern = re.compile(source["asset"])
    for asset in assets:
        name = asset.get("name") or ""
        if pattern.search(name):
            return Resolved(
                download_url=asset["browser_download_url"],
                filename=name,
                version=version,
                source_kind="github",
            )
    names = [a.get("name") for a in assets]
    raise ToolError(f"no asset in {repo}@{version} matches {source['asset']!r} (assets: {names})")


def _resolve_nexus(source: dict[str, Any], client: NexusClient) -> Resolved:
    domain = source["domain"]
    mod_id = int(source["mod_id"])
    file_spec = source.get("file") or "latest-main"

    if file_spec == "latest-main":
        body = client._get_v1_json(f"/v1/games/{domain}/mods/{mod_id}/files.json")
        files = body.get("files") if isinstance(body, dict) else None
        mains = [f for f in (files or []) if f.get("category_name") == "MAIN"]
        if not mains:
            raise ToolError(f"no MAIN file for {domain}/{mod_id} (has this mod ever set one?)")
        info = max(mains, key=lambda f: f.get("uploaded_timestamp") or 0)
    else:
        info = client.mod_file_info(domain, mod_id, int(file_spec))

    file_id = int(info["file_id"])
    version = str(info.get("version") or info.get("mod_version") or file_id)
    filename = info.get("file_name") or f"{domain}-{mod_id}-{file_id}"
    url = client.mod_file_download_url(domain, mod_id, file_id)
    return Resolved(
        download_url=url,
        filename=filename,
        version=version,
        source_kind="nexus",
        nexus_domain=domain,
        nexus_mod_id=mod_id,
        nexus_file_id=file_id,
    )


def _resolve_direct(source: dict[str, Any]) -> Resolved:
    url = source["url"]
    filename = url.rsplit("/", 1)[-1].split("?")[0] or "download"
    version = source.get("sha256") or filename
    return Resolved(download_url=url, filename=filename, version=version, source_kind="direct")


def resolve_source(entry: dict[str, Any], client: NexusClient) -> Resolved:
    source = entry.get("source")
    if not source:
        raise ToolError(f"{entry['id']}: no installable source")
    kind = source.get("type")
    if kind == "github":
        return _resolve_github(source)
    if kind == "nexus":
        return _resolve_nexus(source, client)
    if kind == "direct":
        return _resolve_direct(source)
    raise ToolError(f"{entry['id']}: unknown source type {kind!r}")


# -- Extraction -------------------------------------------------------------------------


def _flatten_single_root(temp_dir: Path) -> Path:
    entries = list(temp_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return temp_dir


def _extract_tool(archive: Path, entry: dict[str, Any], tool_dir: Path) -> None:
    strip = bool((entry.get("install") or {}).get("strip_single_root"))
    with tempfile.TemporaryDirectory(prefix=f"c2wj-tool-{entry['id']}-") as tmp:
        tmp_path = Path(tmp)
        extract(archive, tmp_path)
        root = _flatten_single_root(tmp_path) if strip else tmp_path
        if tool_dir.exists():
            shutil.rmtree(tool_dir)
        tool_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(tool_dir))


# -- ModOrganizer.ini merge (line-based, mirrors build.py/profile.py conventions) -----


def _fwd(path: str) -> str:
    return path.replace("\\", "/")


def _decode_bytearray(value: str) -> str:
    """Decode MO2's `@ByteArray(...)` (backslashes doubled) or a plain path value."""
    value = value.strip()
    if value.startswith("@ByteArray(") and value.endswith(")"):
        value = value[len("@ByteArray(") : -1]
    value = value.replace("\\\\", "\\")
    return _fwd(value)


def read_game_path(ini_path: Path) -> str:
    """Read and decode `gamePath=` from `[General]` in an existing ModOrganizer.ini."""
    if not ini_path.exists():
        return ""
    section = ""
    for line in ini_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "General" and stripped.startswith("gamePath="):
            value = stripped[len("gamePath=") :]
            return _decode_bytearray(value) if value else ""
    return ""


def _new_executable_lines(index: int, block: dict[str, str]) -> list[str]:
    return [
        f"{index}\\title={block['title']}",
        f"{index}\\binary={block['binary']}",
        f"{index}\\arguments={block['arguments']}",
        f"{index}\\workingDirectory={block['workingDirectory']}",
        f"{index}\\hide=false",
        f"{index}\\ownicon=true",
        f"{index}\\steamAppID=",
        f"{index}\\toolbar=true",
    ]


def merge_executables(ini_path: Path, new_blocks: list[dict[str, str]]) -> list[str]:
    """Append `new_blocks` to `[customExecutables]` in `ini_path`.

    Deduped by (binary path, arguments) case-insensitively on the binary -- not by binary
    path alone, since a single catalogue tool (xEdit) intentionally registers two entries
    for the same binary with different arguments (`-sse` vs `-sse -quickautoclean`); only
    an exact binary+arguments repeat (e.g. re-running install for a tool already
    registered) counts as a duplicate.

    Preserves every other line verbatim (line-based, not configparser -- see the module
    docstring for why). Returns the titles of the blocks actually added.
    """
    if not new_blocks:
        return []

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    section = ""
    in_ce = False
    max_idx = 0
    existing_keys: set[tuple[str, str]] = set()
    pending_binary: dict[int, str] = {}
    pending_arguments: dict[int, str] = {}
    size_line_idx: int | None = None
    last_ce_idx: int | None = None
    has_ce_section = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            in_ce = section == "customExecutables"
            if in_ce:
                has_ce_section = True
            out.append(line)
            if in_ce:
                last_ce_idx = len(out) - 1
            continue

        if in_ce:
            if stripped.startswith("size="):
                size_line_idx = len(out)
                out.append(line)
                last_ce_idx = len(out) - 1
                continue
            m = re.match(r"^(\d+)\\binary=(.*)$", line)
            if m:
                idx = int(m.group(1))
                max_idx = max(max_idx, idx)
                pending_binary[idx] = m.group(2).strip()
            else:
                m = re.match(r"^(\d+)\\arguments=(.*)$", line)
                if m:
                    idx = int(m.group(1))
                    max_idx = max(max_idx, idx)
                    pending_arguments[idx] = m.group(2).strip()
            out.append(line)
            last_ce_idx = len(out) - 1
            continue

        out.append(line)

    for idx, binary in pending_binary.items():
        existing_keys.add((binary.lower(), pending_arguments.get(idx, "")))

    to_add: list[tuple[int, dict[str, str]]] = []
    idx = max_idx
    for block in new_blocks:
        key = (block["binary"].strip().lower(), block["arguments"].strip())
        if key in existing_keys:
            continue
        idx += 1
        existing_keys.add(key)
        to_add.append((idx, block))

    if not to_add:
        return []

    new_lines: list[str] = []
    for i, block in to_add:
        new_lines.extend(_new_executable_lines(i, block))

    if not has_ce_section:
        # No [customExecutables] section at all (shouldn't happen for an MO2-written
        # ini, but be defensive): append a fresh one at EOF.
        out.append("")
        out.append("[customExecutables]")
        out.append(f"size={len(to_add)}")
        out.extend(new_lines)
    else:
        if size_line_idx is not None:
            out[size_line_idx] = f"size={max_idx + len(to_add)}"
        insert_at = (last_ce_idx if last_ce_idx is not None else len(out) - 1) + 1
        out[insert_at:insert_at] = new_lines

    ini_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return [b["title"] for _, b in to_add]


def build_executable_blocks(
    entry: dict[str, Any], tool_dir: Path, game_path: str
) -> list[dict[str, str]]:
    tool_dir_fwd = _fwd(str(tool_dir.resolve()))
    blocks: list[dict[str, str]] = []
    for exe in entry.get("executables") or []:
        binary_rel = exe["binary"]
        working = exe.get("workingDirectory") or ""
        if working == "tool":
            working_dir = tool_dir_fwd
        elif working == "game":
            working_dir = game_path
        else:
            working_dir = ""
        blocks.append(
            {
                "title": exe.get("title") or entry["name"],
                "binary": f"{tool_dir_fwd}/{_fwd(binary_rel)}",
                "arguments": exe.get("arguments") or "",
                "workingDirectory": working_dir,
            }
        )
    return blocks


# -- downloads/ .meta sidecar (mirrors downloader.py's MO2/Wabbajack .meta shape) -----


def _write_tool_meta(
    meta_path: Path,
    *,
    entry: dict[str, Any],
    resolved: Resolved,
) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # preserve key case
    general = {
        "gameName": resolved.nexus_domain or "",
        "modID": str(resolved.nexus_mod_id or ""),
        "fileID": str(resolved.nexus_file_id or ""),
        "url": "" if resolved.source_kind == "nexus" else resolved.download_url,
        "name": resolved.filename,
        "description": "",
        "modName": entry["name"],
        "version": resolved.version,
        "newestVersion": "",
        "fileCategory": "",
        "category": "",
        "repository": "Nexus" if resolved.source_kind == "nexus" else "",
        "installed": "false",
        "uninstalled": "false",
        "paused": "false",
        "removed": "false",
    }
    if resolved.source_kind != "nexus":
        general["directURL"] = resolved.download_url
    cfg["General"] = general
    with open(meta_path, "w", encoding="utf-8") as fh:
        cfg.write(fh, space_around_delimiters=False)


# -- Listing --------------------------------------------------------------------------


def _requires_line(entry: dict[str, Any]) -> str:
    requires = entry.get("requires") or []
    if requires:
        return "; ".join(requires)
    note = (entry.get("install") or {}).get("note")
    return note or ""


def _tool_status(entry: dict[str, Any], mo2_dir: Path | None, ledger_data: dict[str, Any]) -> str:
    if _is_disabled(entry):
        return "unavailable"
    if mo2_dir is None:
        return "not installed"
    record = (ledger_data.get("tools") or {}).get(entry["id"])
    tool_dir = mo2_dir / "Tools" / entry["id"]
    if record and tool_dir.exists():
        return "installed"
    return "not installed"


def _companion_status(
    companion_id: str, tool_record: dict[str, Any] | None, mo2_dir: Path | None
) -> str:
    if mo2_dir is None or tool_record is None:
        return "not installed"
    installed = {c.get("id"): c for c in (tool_record.get("companion_mods") or [])}
    rec = installed.get(companion_id)
    if rec and (mo2_dir / "mods" / rec.get("folder", "")).is_dir():
        return "installed"
    return "not installed"


def cmd_tools_list(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    companion_catalog = load_companion_catalog()
    companion_by_id = _catalog_by_id(companion_catalog)
    mo2_dir = Path(args.mo2_dir).resolve() if args.mo2_dir else None
    led = ledger_mod.load(mo2_dir) if mo2_dir else None
    ledger_data: dict[str, Any] = led.data if led is not None else {"tools": {}}

    rows = []
    comp_rows: list[tuple[str, str, str, str]] = []
    for entry in catalog:
        size = entry.get("size_hint_mb")
        size_s = f"{size} MB" if size is not None else "-"
        rows.append(
            (
                entry["id"],
                entry["name"],
                entry["group"],
                "yes" if entry.get("default") else "no",
                size_s,
                _tool_status(entry, mo2_dir, ledger_data),
                _requires_line(entry),
            )
        )
        tool_record = (ledger_data.get("tools") or {}).get(entry["id"])
        for companion_id in entry.get("companion_mods") or []:
            companion = companion_by_id.get(companion_id)
            name = companion["name"] if companion else companion_id
            comp_rows.append(
                (
                    entry["id"],
                    companion_id,
                    name,
                    _companion_status(companion_id, tool_record, mo2_dir),
                )
            )

    headers = ("id", "name", "group", "default", "size", "status", "requires")
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))

    if comp_rows:
        comp_headers = ("tool", "companion id", "name", "status")
        comp_widths = [
            max(len(comp_headers[i]), *(len(r[i]) for r in comp_rows)) for i in range(4)
        ]
        print()
        print("companion mods:")
        print("  ".join(h.ljust(comp_widths[i]) for i, h in enumerate(comp_headers)))
        print("  ".join("-" * w for w in comp_widths))
        for row in comp_rows:
            print("  ".join(str(c).ljust(comp_widths[i]) for i, c in enumerate(row)))
    return 0


# -- Install --------------------------------------------------------------------------


def _client() -> NexusClient:
    from dotenv import load_dotenv

    load_dotenv()
    key = os.environ.get("NEXUS_API_KEY") or None
    return NexusClient(api_key=key)


def _companion_tag(companion_id: str, version: str) -> str:
    """`led.data["mods"][folder]["tag"]`, so a re-run can tell whether the same
    companion mod version is already installed without re-downloading anything."""
    return f"companion:{companion_id}@{version}"


def _install_companion_mods(
    entry: dict[str, Any],
    mo2_dir: Path,
    client: NexusClient,
    led: ledger_mod.Ledger,
    force: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Install a tool's `companion_mods` into `<mo2_dir>/mods/`.

    Reuses exactly the archive -> layout/FOMOD-default machinery `c2wj install` uses
    for a collection's own mods (`installer.install_single_mod`), just without a
    manifest behind it. Returns `(records, ok)`: `records` is what to store under
    `led.data["tools"][tool_id]["companion_mods"]` (one dict per companion mod
    installed or already present), `ok` is False if any companion mod failed.
    """
    tool_id = entry["id"]
    companion_ids = entry.get("companion_mods") or []
    if not companion_ids:
        return [], True

    catalog = load_companion_catalog()
    by_id = _catalog_by_id(catalog)
    owner = ledger_mod.tool_owner(tool_id)
    mods_dir = mo2_dir / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = mo2_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = mo2_dir / ".c2wj-tools-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    game_name = (led.data.get("game") or {}).get("mo2_name") or ""

    records: list[dict[str, Any]] = []
    ok = True
    for companion_id in companion_ids:
        companion = by_id.get(companion_id)
        if companion is None:
            print(f"[{tool_id}] warning: unknown companion mod id {companion_id!r}, skipped")
            ok = False
            continue

        try:
            resolved = resolve_source(companion, client)
        except AuthRequired as e:
            print(f"[{tool_id}] error resolving companion {companion_id}: {e}", file=sys.stderr)
            ok = False
            continue
        except (NexusError, ToolError, requests.RequestException) as e:
            print(f"[{tool_id}] error resolving companion {companion_id}: {e}", file=sys.stderr)
            ok = False
            continue

        folder = sanitize_folder_name(companion.get("name") or companion_id)
        tag = _companion_tag(companion_id, resolved.version)
        dest_root = mods_dir / folder
        existing = led.data.get("mods", {}).get(folder)
        if existing and existing.get("tag") == tag and dest_root.is_dir() and not force:
            print(f"[{tool_id}] companion already installed: {companion['name']} {resolved.version}")
            led.add_mod_owner(folder, owner)
            records.append(
                {
                    "id": companion_id,
                    "name": companion.get("name") or companion_id,
                    "folder": folder,
                    "version": resolved.version,
                }
            )
            continue

        print(f"[{tool_id}] companion: {companion['name']} {resolved.version}")
        cache_dest = CACHE_DIR / f"{companion_id}-{resolved.filename}"
        try:
            archive = _download_cached(resolved.download_url, cache_dest)
        except requests.RequestException as e:
            print(f"[{tool_id}] error downloading companion {companion_id}: {e}", file=sys.stderr)
            ok = False
            continue

        result = installer_mod.install_single_mod(
            archive=archive,
            mods_dir=mods_dir,
            tmp_root=tmp_root,
            name=companion.get("name") or companion_id,
            folder=folder,
            game_name=game_name,
            owner=owner,
            fomod=companion.get("install") == "fomod-defaults",
            choices=companion.get("choices"),
            mod_id=resolved.nexus_mod_id,
            file_id=resolved.nexus_file_id,
            file_name=resolved.filename,
            version=resolved.version,
            force=force,
        )
        if result.failed:
            detail = result.warnings[-1] if result.warnings else "unknown error"
            print(f"[{tool_id}] companion {companion['name']} failed: {detail}", file=sys.stderr)
            ok = False
            continue
        for warning in result.warnings:
            print(f"[{tool_id}] companion {companion['name']}: {warning}")

        led.set_mod_owner(
            folder,
            owner,
            tag=tag,
            install_mode="fresh",
            strategy=result.strategy,
            plugins=result.plugins,
        )

        dl_dest = downloads_dir / resolved.filename
        if not dl_dest.exists():
            shutil.copyfile(archive, dl_dest)
        _write_tool_meta(dl_dest.with_name(dl_dest.name + ".meta"), entry=companion, resolved=resolved)

        print(f"[{tool_id}] companion installed: {companion['name']} -> {dest_root} ({result.strategy})")
        records.append(
            {
                "id": companion_id,
                "name": companion.get("name") or companion_id,
                "folder": folder,
                "version": resolved.version,
                "mod_id": resolved.nexus_mod_id,
                "file_id": resolved.nexus_file_id,
                "strategy": result.strategy,
            }
        )

    shutil.rmtree(tmp_root, ignore_errors=True)
    return records, ok


def _install_one(
    entry: dict[str, Any],
    mo2_dir: Path,
    client: NexusClient,
    led: ledger_mod.Ledger,
    force: bool,
) -> bool:
    """Install one catalogue entry (and its companion mods) into `mo2_dir`.

    Returns True on success (including a plain skip); the ledger (`led`) is updated
    in place but not saved -- the caller saves once after every id in the run.
    """
    tool_id = entry["id"]

    if _is_disabled(entry):
        note = (entry.get("install") or {}).get("note") or "not yet installable"
        print(f"[{tool_id}] skipping: {note}")
        return True

    requires = entry.get("requires") or []
    if requires:
        print(f"[{tool_id}] requires: {'; '.join(requires)}")

    try:
        resolved = resolve_source(entry, client)
    except AuthRequired as e:
        print(f"[{tool_id}] error: {e}", file=sys.stderr)
        return False
    except (NexusError, ToolError, requests.RequestException) as e:
        print(f"[{tool_id}] error resolving source: {e}", file=sys.stderr)
        return False

    tool_dir = mo2_dir / "Tools" / tool_id
    record = (led.data.get("tools") or {}).get(tool_id)
    already_current = (
        record and record.get("version") == resolved.version and tool_dir.exists() and not force
    )
    if already_current:
        print(f"[{tool_id}] already installed (version {resolved.version}); skipping")
    else:
        print(f"[{tool_id}] {entry['name']} {resolved.version}")
        cache_dest = CACHE_DIR / f"{tool_id}-{resolved.filename}"
        try:
            # Nexus mod-file download links returned by mod_file_download_url are
            # already pre-signed, time-limited CDN URLs -- no apikey header needed.
            archive = _download_cached(resolved.download_url, cache_dest)
        except requests.RequestException as e:
            print(f"[{tool_id}] error downloading: {e}", file=sys.stderr)
            return False

        try:
            _extract_tool(archive, entry, tool_dir)
        except RuntimeError as e:
            print(f"[{tool_id}] error extracting: {e}", file=sys.stderr)
            return False

        missing = []
        for exe in entry.get("executables") or []:
            if not (tool_dir / exe["binary"]).exists():
                missing.append(exe["binary"])
        if missing:
            print(f"[{tool_id}] warning: executable(s) not found after install: {missing}")

        downloads_dir = mo2_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        dl_dest = downloads_dir / resolved.filename
        if not dl_dest.exists():
            shutil.copyfile(archive, dl_dest)
        meta_path = dl_dest.with_name(dl_dest.name + ".meta")
        _write_tool_meta(meta_path, entry=entry, resolved=resolved)

        print(f"[{tool_id}] installed -> {tool_dir}")

    game_path = read_game_path(mo2_dir / "ModOrganizer.ini")
    blocks = build_executable_blocks(entry, tool_dir, game_path)
    ini_path = mo2_dir / "ModOrganizer.ini"
    if ini_path.exists() and blocks:
        added = merge_executables(ini_path, blocks)
        if added:
            print(f"[{tool_id}] registered executable(s) in ModOrganizer.ini: {', '.join(added)}")
        else:
            print(f"[{tool_id}] executable(s) already registered in ModOrganizer.ini")
    elif not ini_path.exists():
        print(f"[{tool_id}] warning: {ini_path} not found; skipped registering executables")

    companion_records, companions_ok = _install_companion_mods(entry, mo2_dir, client, led, force)

    led.data.setdefault("tools", {})[tool_id] = {
        "name": entry["name"],
        "version": resolved.version,
        "source": entry.get("source"),
        "dir": str(tool_dir),
        "executables": entry.get("executables") or [],
        "companion_mods": companion_records,
        "installed": datetime.now(UTC).isoformat(),
    }

    return companions_ok


def cmd_tools_install(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    by_id = _catalog_by_id(catalog)
    mo2_dir = Path(args.mo2_dir).resolve()
    if not mo2_dir.exists():
        print(f"error: {mo2_dir} does not exist", file=sys.stderr)
        return 1

    ids: list[str] = list(dict.fromkeys(args.ids or []))
    if args.all_default:
        for e in catalog:
            if e.get("default") and e["id"] not in ids:
                ids.append(e["id"])

    if not ids:
        print("error: no tool ids given (pass ids, and/or --all-default)", file=sys.stderr)
        return 2

    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"error: unknown tool id(s): {', '.join(unknown)}", file=sys.stderr)
        print("see: c2wj tools list", file=sys.stderr)
        return 2

    client = _client()
    led = ledger_mod.load(mo2_dir)

    ok = True
    has_companions = False
    for tool_id in ids:
        entry = by_id[tool_id]
        if entry.get("companion_mods"):
            has_companions = True
        if not _install_one(entry, mo2_dir, client, led, force=args.force):
            ok = False
    led.save()

    if has_companions and led.data.get("layers"):
        # Companion mods just landed in mods/ with no `items` entry of their own (see
        # profile.py's render_instance); re-render so they show up enabled in
        # modlist.txt, above the collection block and below the user's own mods.
        from . import profile as profile_mod  # local: profile.py imports this module

        try:
            profile_mod.render_instance(mo2_dir, led=led)
            print("profile re-rendered so companion mods show up in modlist.txt")
        except (OSError, ValueError) as e:
            print(f"warning: profile could not be re-rendered: {e}", file=sys.stderr)

    return 0 if ok else 1


def cmd_tools_remove(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    by_id = _catalog_by_id(catalog)
    mo2_dir = Path(args.mo2_dir).resolve()
    if not mo2_dir.exists():
        print(f"error: {mo2_dir} does not exist", file=sys.stderr)
        return 1

    ids: list[str] = list(dict.fromkeys(args.ids or []))
    if not ids:
        print("error: no tool ids given", file=sys.stderr)
        return 2

    led = ledger_mod.load(mo2_dir)
    any_removed = False
    for tool_id in ids:
        record = (led.data.get("tools") or {}).get(tool_id)
        if record is None:
            print(f"[{tool_id}] not installed, nothing to remove")
            continue

        tool_dir = mo2_dir / "Tools" / tool_id
        if tool_dir.is_dir():
            shutil.rmtree(tool_dir, ignore_errors=True)
            print(f"[{tool_id}] removed {tool_dir}")

        entry = by_id.get(tool_id) or {"executables": record.get("executables") or []}
        blocks = build_executable_blocks(entry, mo2_dir / "Tools" / tool_id, "")
        keys = {(b["binary"].strip().lower(), b["arguments"].strip()) for b in blocks}
        ini_path = mo2_dir / "ModOrganizer.ini"
        if keys and ini_path.exists():
            from . import layers as layers_mod  # local: layers.py does not import tools.py

            removed_titles = layers_mod.drop_executables(ini_path, keys)
            for title in removed_titles:
                print(f"[{tool_id}] executable removed from ModOrganizer.ini: {title}")

        owner = ledger_mod.tool_owner(tool_id)
        for companion in record.get("companion_mods") or []:
            folder = companion.get("folder")
            if not folder:
                continue
            remaining = led.remove_mod_owner(folder, owner)
            if remaining:
                target = mo2_dir / "mods" / folder
                if target.is_dir():
                    installer_mod.stamp_owner(target, ", ".join(remaining))
                print(f"[{tool_id}] companion kept (still owned by {', '.join(remaining)}): {folder}")
            else:
                shutil.rmtree(mo2_dir / "mods" / folder, ignore_errors=True)
                print(f"[{tool_id}] companion removed: {folder}")

        led.data.get("tools", {}).pop(tool_id, None)
        any_removed = True
        print(f"[{tool_id}] removed")

    led.save()

    if any_removed and led.data.get("layers"):
        from . import profile as profile_mod  # local: profile.py imports this module

        try:
            profile_mod.render_instance(mo2_dir, led=led)
            print("profile re-rendered")
        except (OSError, ValueError) as e:
            print(f"warning: profile could not be re-rendered: {e}", file=sys.stderr)

    return 0


# -- CLI wiring -------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("tools", help="install optional modding tools into an MO2 instance")
    tool_sub = p.add_subparsers(dest="tools_cmd", required=True)

    lp = tool_sub.add_parser("list", help="list the modding tools catalogue")
    lp.add_argument(
        "--mo2-dir",
        default=None,
        help="portable MO2 instance directory (shown installed/not installed against it)",
    )
    lp.set_defaults(func=cmd_tools_list)

    ip = tool_sub.add_parser("install", help="download and register modding tools")
    ip.add_argument("ids", nargs="*", help="tool ids to install (see `c2wj tools list`)")
    ip.add_argument("--mo2-dir", required=True, help="portable MO2 instance directory")
    ip.add_argument(
        "--all-default",
        action="store_true",
        default=False,
        help="also install every catalogue entry with default=true",
    )
    ip.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="reinstall even if the same version is already recorded as installed",
    )
    ip.set_defaults(func=cmd_tools_install)

    rp = tool_sub.add_parser(
        "remove", help="remove installed modding tools and their companion mods"
    )
    rp.add_argument("ids", nargs="+", help="tool ids to remove (see `c2wj tools list`)")
    rp.add_argument("--mo2-dir", required=True, help="portable MO2 instance directory")
    rp.set_defaults(func=cmd_tools_remove)
