"""Download optional modding tools into a generated MO2 instance.

`c2mo2 tools list` prints the catalogue (`tools_catalog.json`, next to this module) with
installed/not-installed status. `c2mo2 tools install <id...> --mo2-dir <instance>` resolves
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
  layout/FOMOD-default machinery `c2mo2 install` uses for a collection's own mods, just
  without a manifest behind it -- owned by `tool:<id>` in the ledger, and re-renders the
  profile (`profile.render_instance`) so they show up enabled in `modlist.txt`. A
  companion mod that one of the instance's collection layers already pins (same Nexus
  mod id in the layer's manifest) is *not* installed: the collection's copy is the
  version its curator generated LOD against, and the catalogue's newest-main copy would
  sit above the collection in MO2 and override it (`_collection_nexus_mods`); and
- records the install in `<mo2-dir>/c2mo2-instance.json` under `tools[id]` (via
  `ledger.py`, so companion mods land in the same `mods` map a collection's do).

`c2mo2 tools remove <id...> --mo2-dir <instance>` is the inverse: deletes `Tools/<id>`,
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

from . import create as create_mod
from . import installer as installer_mod
from . import ledger as ledger_mod
from .manifest import load_manifest
from .naming import sanitize_folder_name
from .nexus import USER_AGENT, AuthRequired, NexusClient, NexusError
from .reporter import get_reporter
from .sevenzip import extract

# Repo root is two levels above this file: src/collections2mo2/tools.py
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "tools" / "cache"
CATALOG_PATH = Path(__file__).resolve().parent / "tools_catalog.json"

GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
_CHUNK_SIZE = 1 << 20  # 1 MiB
LEDGER_NAME = ledger_mod.LEDGER_NAME


def _open_instance(mo2_dir: Path) -> None:
    """Bring a pre-rename (`c2wj`) instance onto its current names before we touch it."""
    create_mod.migrate_legacy_instance(create_mod.Paths(mo2_dir), get_reporter(None))


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
    with tempfile.TemporaryDirectory(prefix=f"c2mo2-tool-{entry['id']}-") as tmp:
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


def _to_windows_path(path: str) -> str:
    return path.replace("/", "\\")


def _qt_escape(value: str) -> str:
    """MO2 stores a `customExecutables` `arguments` value the way Qt's IniFormat writer
    escapes any string: every backslash doubled, then every quote escaped with a
    backslash. Verified against a working Wabbajack Stock Game instance (Lorerim): the
    logical argument `-D:"D:\\Lorerim\\Stock Game\\Data"` is stored on disk as
    `-D:\\"D:\\\\Lorerim\\\\Stock Game\\\\Data\\"` (mirrored by
    `build._qt_escape` -- kept local here too, see the module docstring on why tools.py
    stays self-contained from build.py)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _expand_arg_template(template: str, *, game_path: str, tool_dir_fwd: str) -> str:
    """Expand a catalogue executable's `arguments` template (`{game}`, `{game_data}`,
    `{tool}`) and Qt-escape the result for MO2's ini.

    `{game_data}` is `<game_path>\\Data` -- `game_path` is the decoded `gamePath=` from
    ModOrganizer.ini (the Stock Game copy when `build --stock-game` made one), so this
    is how xEdit/DynDOLOD/TexGen-family tools are told to read the VFS-managed Data
    folder via `-D:"{game_data}"` instead of falling back to their own registry-derived
    game install (see the module's `tools_catalog.json` entries). Paths are rendered
    Windows-style (backslash) to match the reference Wabbajack Stock Game instance this
    was verified against. Empty if `game_path` is not yet known -- callers should not
    register a `-D:` tool before ModOrganizer.ini has a `gamePath`.
    """
    if not template:
        return ""
    game_win = _to_windows_path(game_path) if game_path else ""
    game_data_win = f"{game_win}\\Data" if game_win else ""
    tool_win = _to_windows_path(tool_dir_fwd)
    expanded = template.format(game=game_win, game_data=game_data_win, tool=tool_win)
    return _qt_escape(expanded)


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


_CE_ANY_KEY_RE = re.compile(r"^(\d+)\\(\w+)=(.*)$")


def _parse_customexecutables(
    lines: list[str],
) -> tuple[list[str], bool, dict[int, dict[str, str]], list[int], int | None, list[str]]:
    """Shared line-based parse of `[customExecutables]` used by `replace_executables`
    and `_drop_executables_by_binary`: everything outside the section is kept verbatim
    in `out` (including the section's own header and `size=` line, at `size_line_idx`);
    each numbered entry becomes `blocks[idx]` (insertion order in `order`); any stray
    non-`N\\key=value` line inside the section is kept in `tail` and re-appended after
    the rebuilt entries. Returns `(out, has_ce_section, blocks, order, size_line_idx, tail)`.
    """
    out: list[str] = []
    in_ce = False
    has_ce_section = False
    blocks: dict[int, dict[str, str]] = {}
    order: list[int] = []
    size_line_idx: int | None = None
    tail: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_ce = stripped[1:-1] == "customExecutables"
            if in_ce:
                has_ce_section = True
            out.append(line)
            continue
        if in_ce:
            if stripped.startswith("size="):
                size_line_idx = len(out)
                out.append(line)
                continue
            m = _CE_ANY_KEY_RE.match(line)
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

    return out, has_ce_section, blocks, order, size_line_idx, tail


def replace_executables(ini_path: Path, new_blocks: list[dict[str, str]]) -> list[str]:
    """Rewrite `[customExecutables]` entries in `ini_path` in place, matched by title.

    The `tools refresh` counterpart of `merge_executables` (which dedups by
    `(binary, arguments)` and only ever appends): here, an existing entry whose title
    matches one of `new_blocks` has its `binary`/`arguments`/`workingDirectory`
    overwritten with the recomputed values -- so a stale `-D:"<old game path>"` baked
    into `arguments` (e.g. by a `build --stock-game` that ran after the tool was
    installed, or an instance installed before this catalogue added `-D:` at all) gets
    corrected in place instead of leaving a duplicate entry. `hide`/`ownicon`/
    `steamAppID`/`toolbar` and any other keys are left untouched (a user may have
    changed them). A `new_blocks` entry whose title has no existing match is appended,
    same as `merge_executables`. Returns the titles changed (updated in place or newly
    added); an empty list if `ini_path` has no `[customExecutables]` section to update
    (fresh registration goes through `merge_executables`/`_install_one` instead).
    """
    if not new_blocks or not ini_path.exists():
        return []
    by_title = {b["title"]: b for b in new_blocks}

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    out, has_ce_section, blocks, order, size_line_idx, tail = _parse_customexecutables(lines)
    if not has_ce_section:
        return []

    changed: list[str] = []
    for idx in order:
        title = blocks[idx].get("title", "")
        block = by_title.get(title)
        if block is None:
            continue
        for key in ("binary", "arguments", "workingDirectory"):
            new_value = block[key]
            if blocks[idx].get(key) != new_value:
                blocks[idx][key] = new_value
                if title not in changed:
                    changed.append(title)

    existing_titles = {blocks[idx].get("title", "") for idx in order}
    to_add = [b for b in new_blocks if b["title"] not in existing_titles]

    idx = max(order, default=0)
    for block in to_add:
        idx += 1
        blocks[idx] = {
            "title": block["title"],
            "binary": block["binary"],
            "arguments": block["arguments"],
            "workingDirectory": block["workingDirectory"],
            "hide": "false",
            "ownicon": "true",
            "steamAppID": "",
            "toolbar": "true",
        }
        order.append(idx)
        changed.append(block["title"])

    if not changed:
        return []

    rendered: list[str] = []
    for i in order:
        for key, value in blocks[i].items():
            rendered.append(f"{i}\\{key}={value}")
    rendered.extend(tail)
    rendered.append("")

    if size_line_idx is not None:
        out[size_line_idx] = f"size={len(order)}"
        out[size_line_idx + 1 : size_line_idx + 1] = rendered
    else:
        out.append(f"size={len(order)}")
        out.extend(rendered)

    ini_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return changed


def _drop_executables_by_binary(ini_path: Path, binaries: set[str]) -> list[str]:
    """Remove `[customExecutables]` entries whose `binary` (case-insensitive) is in
    `binaries`; renumber the rest.

    Tools-local counterpart of `layers.drop_executables` (which matches by exact
    `(binary, arguments)` -- unsuitable here now that `arguments` can embed a dynamic
    `-D:"<game path>"`, so a `cmd_tools_remove` that recomputes blocks without a game
    path would never match the real stored entry). Matching on the binary alone is
    correct anyway: removal deletes the whole tool, so every entry pointing at one of
    its binaries should go regardless of what arguments it currently carries. Returns
    the titles removed.
    """
    binaries_lower = {b.strip().lower() for b in binaries}
    if not binaries_lower or not ini_path.exists():
        return []

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    out, has_ce_section, blocks, order, size_line_idx, tail = _parse_customexecutables(lines)
    if not has_ce_section:
        return []

    kept = [i for i in order if blocks[i].get("binary", "").strip().lower() not in binaries_lower]
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
        out.append(f"size={len(kept)}")
        out.extend(rendered)

    ini_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return removed


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
                "arguments": _expand_arg_template(
                    exe.get("arguments") or "", game_path=game_path, tool_dir_fwd=tool_dir_fwd
                ),
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
    if rec and rec.get("provided_by"):
        return f"provided by {rec['provided_by'].get('layer_name') or 'collection'}"
    if rec and (mo2_dir / "mods" / rec.get("folder", "")).is_dir():
        return "installed"
    return "not installed"


def cmd_tools_list(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    companion_catalog = load_companion_catalog()
    companion_by_id = _catalog_by_id(companion_catalog)
    mo2_dir = Path(args.mo2_dir).resolve() if args.mo2_dir else None
    if mo2_dir is not None and mo2_dir.is_dir():
        _open_instance(mo2_dir)
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
        comp_widths = [max(len(comp_headers[i]), *(len(r[i]) for r in comp_rows)) for i in range(4)]
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


def _collection_nexus_mods(mo2_dir: Path, led: ledger_mod.Ledger) -> dict[int, dict[str, Any]]:
    """Nexus mod id -> the first collection layer mod that pins it, across every layer.

    Read from each layer's manifest (`layers[].manifest`, instance-relative). Used to
    keep a tool's companion mod out of an instance whose collection already ships
    that mod: GTS pins DynDOLOD Resources SE and DLL NG at the versions its
    pre-generated LOD was built with, and the catalogue's newest-main copies, placed
    above the collection block, made the DynDOLOD DLL reject the scripts at startup.
    """
    found: dict[int, dict[str, Any]] = {}
    for layer in led.data.get("layers") or []:
        rel = layer.get("manifest")
        if not rel:
            continue
        path = Path(rel)
        path = path if path.is_absolute() else mo2_dir / path
        if not path.exists():
            continue
        try:
            manifest = load_manifest(path)
        except (OSError, ValueError):
            continue
        domain = (manifest.get("info") or {}).get("domainName") or ""
        for mod in manifest.get("mods") or []:
            src = mod.get("source") or {}
            mod_id = src.get("modId")
            if src.get("type", "nexus") != "nexus" or not isinstance(mod_id, int):
                continue
            found.setdefault(
                mod_id,
                {
                    "layer": layer.get("slug"),
                    "revision": layer.get("revision"),
                    "layer_name": layer.get("name") or layer.get("slug") or "",
                    "domain": domain,
                    "mod": mod.get("name") or "",
                    "version": str(mod.get("version") or ""),
                    "optional": bool(mod.get("optional")),
                },
            )
    return found


def _provided_by_collection(
    companion: dict[str, Any], collection_mods: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    """The collection-layer mod that already pins `companion`'s Nexus mod, if any."""
    src = companion.get("source") or {}
    if src.get("type") != "nexus":
        return None
    provided = collection_mods.get(src.get("mod_id"))
    if provided is None:
        return None
    if src.get("domain") and provided["domain"] and provided["domain"] != src["domain"]:
        return None
    return provided


def _install_companion_mods(
    entry: dict[str, Any],
    mo2_dir: Path,
    client: NexusClient,
    led: ledger_mod.Ledger,
    force: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Install a tool's `companion_mods` into `<mo2_dir>/mods/`.

    Reuses exactly the archive -> layout/FOMOD-default machinery `c2mo2 install` uses
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
    tmp_root = mo2_dir / ".c2mo2-tools-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    game_name = (led.data.get("game") or {}).get("mo2_name") or ""
    collection_mods = _collection_nexus_mods(mo2_dir, led)

    records: list[dict[str, Any]] = []
    ok = True
    for companion_id in companion_ids:
        companion = by_id.get(companion_id)
        if companion is None:
            print(f"[{tool_id}] warning: unknown companion mod id {companion_id!r}, skipped")
            ok = False
            continue
        name = companion.get("name") or companion_id

        provided = _provided_by_collection(companion, collection_mods)
        if provided is not None:
            # The collection's pinned copy wins. A copy an earlier run of this tool put
            # in place (sole owner, our companion tag) is removed so the collection's
            # mod is no longer overridden; anything else with that folder name is left.
            folder = sanitize_folder_name(name)
            existing = led.data.get("mods", {}).get(folder) or {}
            our_tag = str(existing.get("tag") or "").startswith(f"companion:{companion_id}@")
            if our_tag and led.owners_of(folder) == [owner]:
                shutil.rmtree(mods_dir / folder, ignore_errors=True)
                led.remove_mod_owner(folder, owner)
                print(f"[{tool_id}] companion removed: {folder} (the collection's copy takes over)")
            pinned = f"{provided['mod']} {provided['version']}".strip()
            print(
                f"[{tool_id}] companion {name}: already provided by "
                f"{provided['layer_name']} ({pinned}); the collection's copy is used"
            )
            records.append({"id": companion_id, "name": name, "provided_by": provided})
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
            print(
                f"[{tool_id}] companion already installed: {companion['name']} {resolved.version}"
            )
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
        _write_tool_meta(
            dl_dest.with_name(dl_dest.name + ".meta"), entry=companion, resolved=resolved
        )

        print(
            f"[{tool_id}] companion installed: {companion['name']} -> {dest_root} ({result.strategy})"
        )
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
    _open_instance(mo2_dir)

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
        print("see: c2mo2 tools list", file=sys.stderr)
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
    _open_instance(mo2_dir)

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
        tool_dir_fwd = _fwd(str((mo2_dir / "Tools" / tool_id).resolve()))
        binaries = {
            f"{tool_dir_fwd}/{_fwd(exe['binary'])}" for exe in (entry.get("executables") or [])
        }
        ini_path = mo2_dir / "ModOrganizer.ini"
        if binaries and ini_path.exists():
            removed_titles = _drop_executables_by_binary(ini_path, binaries)
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
                print(
                    f"[{tool_id}] companion kept (still owned by {', '.join(remaining)}): {folder}"
                )
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


# -- Refresh --------------------------------------------------------------------------


def cmd_tools_refresh(args: argparse.Namespace) -> int:
    """Re-derive `[customExecutables]` entries for already-installed tools from the
    current catalogue and the instance's current `gamePath`, and rewrite them in place
    -- no re-download, no re-extraction.

    For fixing an existing instance whose xEdit/DynDOLOD/TexGen entries predate
    `-D:"{game_data}"` being added to the catalogue (or were registered before
    `build --stock-game` ran and never got rebased): `c2mo2 tools refresh --mo2-dir
    <instance>` rewrites every tool recorded in the ledger; passing ids restricts it to
    those. Tools no longer in the catalogue (or disabled) are refreshed from the
    executables recorded at install time, so a stale entry still gets corrected even if
    the catalogue entry was since removed.
    """
    catalog = load_catalog()
    by_id = _catalog_by_id(catalog)
    mo2_dir = Path(args.mo2_dir).resolve()
    if not mo2_dir.exists():
        print(f"error: {mo2_dir} does not exist", file=sys.stderr)
        return 1
    _open_instance(mo2_dir)

    ini_path = mo2_dir / "ModOrganizer.ini"
    if not ini_path.exists():
        print(f"error: {ini_path} not found", file=sys.stderr)
        return 1

    led = ledger_mod.load(mo2_dir)
    installed = led.data.get("tools") or {}
    ids: list[str] = list(dict.fromkeys(args.ids or list(installed.keys())))
    if not ids:
        print("no tools installed; nothing to refresh")
        return 0

    unknown = [i for i in ids if i not in installed]
    if unknown:
        print(f"error: not installed: {', '.join(unknown)}", file=sys.stderr)
        return 2

    game_path = read_game_path(ini_path)
    if not game_path:
        print(f"warning: {ini_path} has no gamePath yet; -D:-style arguments will be empty")

    any_changed = False
    for tool_id in ids:
        record = installed[tool_id]
        entry = by_id.get(tool_id)
        if entry is None:
            entry = {"id": tool_id, "name": record.get("name") or tool_id}
        if entry.get("executables") is None:
            entry = {**entry, "executables": record.get("executables") or []}

        tool_dir = mo2_dir / "Tools" / tool_id
        blocks = build_executable_blocks(entry, tool_dir, game_path)
        changed = replace_executables(ini_path, blocks)
        if changed:
            any_changed = True
            print(f"[{tool_id}] refreshed: {', '.join(changed)}")
        else:
            print(f"[{tool_id}] already up to date")

    if not any_changed:
        print("nothing to refresh")
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
    ip.add_argument("ids", nargs="*", help="tool ids to install (see `c2mo2 tools list`)")
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
    rp.add_argument("ids", nargs="+", help="tool ids to remove (see `c2mo2 tools list`)")
    rp.add_argument("--mo2-dir", required=True, help="portable MO2 instance directory")
    rp.set_defaults(func=cmd_tools_remove)

    fp = tool_sub.add_parser(
        "refresh",
        help="rewrite already-installed tools' [customExecutables] entries from the "
        "current catalogue/gamePath, without re-downloading",
    )
    fp.add_argument("ids", nargs="*", help="tool ids to refresh (default: every installed tool)")
    fp.add_argument("--mo2-dir", required=True, help="portable MO2 instance directory")
    fp.set_defaults(func=cmd_tools_refresh)
