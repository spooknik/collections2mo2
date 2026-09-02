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
  and
- records the install in `<mo2-dir>/c2wj-instance.json` under `tools[id]`.

This module is deliberately self-contained (no imports from build.py, cli.py,
installer.py, profile.py or ledger.py, which are being developed concurrently): it
re-derives `<repo>/tools/cache/` from its own file location and reimplements the small
cached-download and single-root-flatten helpers rather than importing build.py's private
ones. It does read from nexus.py and sevenzip.py, which are stable.
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

from .nexus import USER_AGENT, AuthRequired, NexusClient, NexusError
from .sevenzip import extract

# Repo root is two levels above this file: src/collections2wabbajack/tools.py
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "tools" / "cache"
CATALOG_PATH = Path(__file__).resolve().parent / "tools_catalog.json"

GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
_CHUNK_SIZE = 1 << 20  # 1 MiB
LEDGER_NAME = "c2wj-instance.json"


class ToolError(RuntimeError):
    pass


# -- Catalogue ------------------------------------------------------------------------


def load_catalog() -> list[dict[str, Any]]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return data["tools"]


def _catalog_by_id(catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in catalog}


def _is_disabled(entry: dict[str, Any]) -> bool:
    return bool((entry.get("install") or {}).get("disabled"))


# -- Ledger (self-contained: no import of ledger.py, see module docstring) ------------


def _load_ledger(mo2_dir: Path) -> dict[str, Any]:
    path = mo2_dir / LEDGER_NAME
    if not path.exists():
        return {"version": 1, "tools": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("tools", {})
    return data


def _save_ledger(mo2_dir: Path, data: dict[str, Any]) -> Path:
    path = mo2_dir / LEDGER_NAME
    mo2_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


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


def _tool_status(entry: dict[str, Any], mo2_dir: Path | None, ledger: dict[str, Any]) -> str:
    if _is_disabled(entry):
        return "unavailable"
    if mo2_dir is None:
        return "not installed"
    record = (ledger.get("tools") or {}).get(entry["id"])
    tool_dir = mo2_dir / "Tools" / entry["id"]
    if record and tool_dir.exists():
        return "installed"
    return "not installed"


def cmd_tools_list(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    mo2_dir = Path(args.mo2_dir).resolve() if args.mo2_dir else None
    ledger = _load_ledger(mo2_dir) if mo2_dir else {"tools": {}}

    rows = []
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
                _tool_status(entry, mo2_dir, ledger),
                _requires_line(entry),
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
    return 0


# -- Install --------------------------------------------------------------------------


def _client() -> NexusClient:
    from dotenv import load_dotenv

    load_dotenv()
    key = os.environ.get("NEXUS_API_KEY") or None
    return NexusClient(api_key=key)


def _install_one(
    entry: dict[str, Any],
    mo2_dir: Path,
    client: NexusClient,
    ledger: dict[str, Any],
    force: bool,
) -> bool:
    """Install one catalogue entry into `mo2_dir`. Returns True on success (incl. skip)."""
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
    record = (ledger.get("tools") or {}).get(tool_id)
    if record and record.get("version") == resolved.version and tool_dir.exists() and not force:
        print(f"[{tool_id}] already installed (version {resolved.version}); skipping")
        return True

    print(f"[{tool_id}] {entry['name']} {resolved.version}")
    cache_dest = CACHE_DIR / f"{tool_id}-{resolved.filename}"
    try:
        # Nexus mod-file download links returned by mod_file_download_url are already
        # pre-signed, time-limited CDN URLs -- no apikey header needed to fetch them.
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

    downloads_dir = mo2_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    dl_dest = downloads_dir / resolved.filename
    if not dl_dest.exists():
        shutil.copyfile(archive, dl_dest)
    meta_path = dl_dest.with_name(dl_dest.name + ".meta")
    _write_tool_meta(meta_path, entry=entry, resolved=resolved)

    ledger.setdefault("tools", {})[tool_id] = {
        "name": entry["name"],
        "version": resolved.version,
        "source": entry.get("source"),
        "dir": str(tool_dir),
        "executables": entry.get("executables") or [],
        "installed": datetime.now(UTC).isoformat(),
    }
    _save_ledger(mo2_dir, ledger)

    print(f"[{tool_id}] installed -> {tool_dir}")
    return True


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
    ledger = _load_ledger(mo2_dir)

    ok = True
    for tool_id in ids:
        entry = by_id[tool_id]
        if not _install_one(entry, mo2_dir, client, ledger, force=args.force):
            ok = False

    return 0 if ok else 1


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
