"""Download the Nexus-hosted mods referenced by a collection manifest.

Streams each pinned file to `<out>/<file_name>`, verifies its MD5 against the
manifest, and writes an MO2-compatible `.meta` sidecar next to it plus a
`downloads.json` summary of the whole run.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .manifest import install_mode, load_manifest
from .nexus import AuthRequired, NexusClient, NexusError

# Nexus domain -> MO2 short game name.
MO2_GAME_NAMES: dict[str, str] = {
    "skyrimspecialedition": "SkyrimSE",
    "skyrim": "Skyrim",
    "skyrimvr": "SkyrimVR",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "falloutnv": "FalloutNV",
    "fallout3": "Fallout3",
    "oblivion": "Oblivion",
    "morrowind": "Morrowind",
    "starfield": "Starfield",
    "enderal": "Enderal",
    "enderalspecialedition": "EnderalSE",
}

CHUNK_SIZE = 1 << 20  # 1 MiB
CHECKPOINT_EVERY = 10


def mo2_game_name(domain: str) -> str:
    name = MO2_GAME_NAMES.get(domain)
    if name is None:
        raise NexusError(
            f"unknown Nexus domain {domain!r}: add it to MO2_GAME_NAMES in downloader.py "
            f"(known domains: {', '.join(sorted(MO2_GAME_NAMES))})"
        )
    return name


@dataclass
class DownloadEntry:
    name: str
    mod_id: int
    file_id: int
    tag: str | None
    md5: str | None
    update_policy: str | None
    install_mode: str
    optional: bool
    phase: Any
    mod_type: str
    file_name: str | None = None
    path: str | None = None
    size: int | None = None
    status: str = "error"
    error: str | None = None


@dataclass
class RunState:
    manifest: str
    domain: str
    game_name: str
    entries: list[DownloadEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "domain": self.domain,
            "game_name": self.game_name,
            "entries": [asdict(e) for e in self.entries],
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")


class _ThreadClients:
    """One NexusClient per worker thread (requests.Session is not thread-safe)."""

    def __init__(self, api_key: str | None):
        self._api_key = api_key
        self._local = threading.local()

    def get(self) -> NexusClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = NexusClient(api_key=self._api_key)
            self._local.client = client
        return client


def _md5_of_file(path: Path, chunk: int = CHUNK_SIZE) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for part in iter(lambda: fh.read(chunk), b""):
            h.update(part)
    return h.hexdigest()


def _write_meta(
    meta_path: Path,
    *,
    game_name: str,
    mod_id: int,
    file_id: int,
    name: str,
    mod_name: str,
    version: str,
    file_category: str,
) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # preserve case of keys
    cfg["General"] = {
        "gameName": game_name,
        "modID": str(mod_id),
        "fileID": str(file_id),
        "url": "",
        "name": name,
        "description": "",
        "modName": mod_name,
        "version": version,
        "newestVersion": "",
        "fileCategory": file_category,
        "category": "",
        "repository": "Nexus",
        "installed": "false",
        "uninstalled": "false",
        "paused": "false",
        "removed": "false",
    }
    with open(meta_path, "w", encoding="utf-8") as fh:
        cfg.write(fh, space_around_delimiters=False)


def _download_one(
    clients: _ThreadClients,
    mod: dict[str, Any],
    out_dir: Path,
    domain: str,
    game_name: str,
) -> DownloadEntry:
    src = mod.get("source") or {}
    mod_id = int(src["modId"])
    file_id = int(src["fileId"])
    expected_md5 = src.get("md5")
    entry = DownloadEntry(
        name=mod.get("name") or "",
        mod_id=mod_id,
        file_id=file_id,
        tag=src.get("tag"),
        md5=expected_md5,
        update_policy=src.get("updatePolicy"),
        install_mode=install_mode(mod),
        optional=bool(mod.get("optional")),
        phase=mod.get("phase", 0),
        mod_type=(mod.get("details") or {}).get("type") or "",
    )

    client = clients.get()
    try:
        info = client.mod_file_info(domain, mod_id, file_id)
    except (NexusError, KeyError, ValueError) as e:
        entry.error = f"file info failed: {e}"
        return entry

    file_name = info.get("file_name") or src.get("logicalFilename") or f"{mod_id}_{file_id}.bin"
    dest = out_dir / file_name
    entry.file_name = file_name
    entry.path = str(dest.resolve())

    # Resumable: if the file's already there and its MD5 matches, skip the download.
    if dest.exists() and expected_md5:
        try:
            existing_md5 = _md5_of_file(dest)
        except OSError as e:
            entry.error = f"could not hash existing file: {e}"
            existing_md5 = None
        if existing_md5 == expected_md5:
            entry.status = "skipped"
            entry.size = dest.stat().st_size
            _write_meta_for(mod, info, dest, game_name, mod_id, file_id)
            return entry

    try:
        url = client.mod_file_download_url(domain, mod_id, file_id)
    except NexusError as e:
        entry.error = f"download link failed: {e}"
        return entry

    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    try:
        h = hashlib.md5()
        out_dir.mkdir(parents=True, exist_ok=True)
        with client.session.get(url, stream=True, timeout=client.timeout) as resp:
            resp.raise_for_status()
            with open(tmp_dest, "wb") as fh:
                for part in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not part:
                        continue
                    fh.write(part)
                    h.update(part)
    except Exception as e:  # noqa: BLE001 - record any download failure, keep going
        entry.error = f"download failed: {e}"
        try:
            if tmp_dest.exists():
                tmp_dest.unlink()
        except OSError:
            pass
        return entry

    actual_md5 = h.hexdigest()
    size = tmp_dest.stat().st_size

    if expected_md5 and actual_md5 != expected_md5:
        bad_dest = out_dir / f"{file_name}.md5mismatch"
        try:
            if bad_dest.exists():
                bad_dest.unlink()
            tmp_dest.replace(bad_dest)
        except OSError as e:
            entry.error = (
                f"md5 mismatch (expected {expected_md5}, got {actual_md5}); "
                f"also failed to rename bad file: {e}"
            )
            entry.status = "md5_mismatch"
            entry.size = size
            return entry
        entry.status = "md5_mismatch"
        entry.error = f"md5 mismatch: expected {expected_md5}, got {actual_md5}"
        entry.size = size
        entry.path = str(bad_dest.resolve())
        return entry

    tmp_dest.replace(dest)
    entry.status = "ok"
    entry.size = size
    _write_meta_for(mod, info, dest, game_name, mod_id, file_id)
    return entry


def _write_meta_for(
    mod: dict[str, Any],
    info: dict[str, Any],
    dest: Path,
    game_name: str,
    mod_id: int,
    file_id: int,
) -> None:
    meta_path = dest.with_name(dest.name + ".meta")
    version = mod.get("version") or info.get("version") or info.get("mod_version") or ""
    _write_meta(
        meta_path,
        game_name=game_name,
        mod_id=mod_id,
        file_id=file_id,
        name=info.get("name") or mod.get("name") or "",
        mod_name=mod.get("name") or "",
        version=str(version),
        # MO2 stores Nexus's numeric file category (1 main, 2 update, 3 optional, 4 old, 5 misc)
        file_category=str(info.get("category_id") or ""),
    )


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def run_download(
    manifest_path: Path,
    out_dir: Path,
    jobs: int,
    limit: int | None,
    include_optional: bool,
    api_key: str | None,
) -> int:
    manifest = load_manifest(manifest_path)
    info = manifest.get("info") or {}
    domain = info.get("domainName")
    mods: list[dict[str, Any]] = manifest.get("mods") or []
    if not domain:
        # Fall back to the domain recorded on the first Nexus mod entry.
        for m in mods:
            d = m.get("domainName")
            if d:
                domain = d
                break
    if not domain:
        print("error: could not determine game domain from manifest", file=sys.stderr)
        return 1

    game_name = mo2_game_name(domain)

    nexus_mods = [m for m in mods if (m.get("source") or {}).get("type") == "nexus"]
    if not include_optional:
        nexus_mods = [m for m in nexus_mods if not m.get("optional")]
    if limit is not None:
        nexus_mods = nexus_mods[:limit]

    total = len(nexus_mods)
    if total == 0:
        print("no Nexus-sourced mods to download")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(manifest=str(manifest_path.resolve()), domain=domain, game_name=game_name)
    clients = _ThreadClients(api_key)

    counts: dict[str, int] = {"ok": 0, "skipped": 0, "md5_mismatch": 0, "error": 0}
    total_bytes = 0
    start = time.monotonic()
    downloads_json = out_dir / "downloads.json"

    print(f"downloading {total} mod(s) for {game_name} ({domain}) with {jobs} worker(s)")

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_download_one, clients, mod, out_dir, domain, game_name): mod
            for mod in nexus_mods
        }
        for done, fut in enumerate(as_completed(futures), start=1):
            mod = futures[fut]
            try:
                entry = fut.result()
            except Exception as e:  # noqa: BLE001 - never let one mod kill the run
                src = mod.get("source") or {}
                entry = DownloadEntry(
                    name=mod.get("name") or "",
                    mod_id=int(src.get("modId") or 0),
                    file_id=int(src.get("fileId") or 0),
                    tag=src.get("tag"),
                    md5=src.get("md5"),
                    update_policy=src.get("updatePolicy"),
                    install_mode=install_mode(mod),
                    optional=bool(mod.get("optional")),
                    phase=mod.get("phase", 0),
                    mod_type=(mod.get("details") or {}).get("type") or "",
                    status="error",
                    error=f"unexpected failure: {e}",
                )
            state.entries.append(entry)
            counts[entry.status] = counts.get(entry.status, 0) + 1
            if entry.size:
                total_bytes += entry.size

            size_str = _fmt_bytes(entry.size) if entry.size else "-"
            status_label = {
                "ok": "ok",
                "skipped": "skip",
                "md5_mismatch": "MISMATCH",
                "error": "ERROR",
            }.get(entry.status, entry.status)
            line = f"[{done}/{total}] {status_label:8s} {entry.file_name or entry.name}  {size_str}"
            if entry.error and entry.status != "skipped":
                line += f"  ({entry.error})"
            print(line)

            if done % CHECKPOINT_EVERY == 0:
                state.write(downloads_json)

    state.write(downloads_json)
    elapsed = time.monotonic() - start

    print()
    print(
        f"done in {elapsed:.1f}s: ok={counts.get('ok', 0)} "
        f"skipped={counts.get('skipped', 0)} "
        f"md5_mismatch={counts.get('md5_mismatch', 0)} "
        f"error={counts.get('error', 0)}  total={_fmt_bytes(total_bytes)}"
    )
    print(f"downloads.json: {downloads_json.resolve()}")

    ok = counts.get("md5_mismatch", 0) == 0 and counts.get("error", 0) == 0
    return 0 if ok else 1


def cmd_download(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = manifest_path.resolve().parent.parent / "downloads"

    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("NEXUS_API_KEY") or None
    if not api_key:
        print(
            "error: NEXUS_API_KEY is required (set it in .env). "
            "See .env.example.",
            file=sys.stderr,
        )
        return 2

    try:
        return run_download(
            manifest_path=manifest_path,
            out_dir=out_dir,
            jobs=args.jobs,
            limit=args.limit,
            include_optional=args.include_optional,
            api_key=api_key,
        )
    except AuthRequired as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except NexusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "download", help="download the Nexus-hosted mods referenced by a collection.json"
    )
    p.add_argument("manifest", help="path to collection.json")
    p.add_argument(
        "--out",
        default=None,
        help="output directory (default: <manifest dir>/../downloads)",
    )
    p.add_argument("--jobs", type=int, default=4, help="parallel download workers (default: 4)")
    p.add_argument("--limit", type=int, default=None, help="only download the first N mods")
    p.add_argument(
        "--include-optional",
        dest="include_optional",
        action="store_true",
        default=True,
        help="include optional mods (default)",
    )
    p.add_argument(
        "--no-optional",
        dest="include_optional",
        action="store_false",
        help="skip mods marked optional",
    )
    p.set_defaults(func=cmd_download)
