"""Download the mods referenced by a collection manifest.

Streams each Nexus-pinned file and each `source.type == "direct"` file to
`<out>/<file_name>`, verifies its MD5 against the manifest, and writes an
MO2/Wabbajack-compatible `.meta` sidecar next to it plus a `downloads.json`
summary of the whole run. Other source types (`browse`, `manual`, `bundle`,
...) are not downloaded; they are recorded with `status="unsupported"`.
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
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

from .manifest import install_mode, load_manifest
from .nexus import USER_AGENT, AuthRequired, NexusClient, NexusError
from .reporter import Reporter, get_reporter

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
DIRECT_TIMEOUT = 60.0
DIRECT_MAX_RETRIES = 3  # additional attempts after the first, with 2/4/8s backoff


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
    mod_id: int | None
    file_id: int | None
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
    source_type: str = "nexus"
    url: str | None = None


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


class _ByteTracker:
    """Thread-safe cumulative bytes-downloaded counter shared across download workers.

    `add_bytes` is called from each worker thread's chunk-write loop and reports a
    throttled `progress()` call about every `REPORT_EVERY` bytes, so a GUI's rate/ETA
    line moves during a single large download instead of jumping only once per file.
    `file_done` is called once per completed file from the single-threaded
    `as_completed` loop in `run_download` and always reports, so the counter and the
    file-level progress line never drift apart.

    `total_bytes` comes from summing the manifest's `source.fileSize` up front (no
    extra network calls) -- when that is unavailable or zero, `bytes_total` is
    reported as `None` rather than a misleading 0.
    """

    REPORT_EVERY = 2 << 20  # ~2 MiB

    def __init__(self, rep: Reporter, total_files: int, total_bytes: int):
        self._rep = rep
        self._lock = threading.Lock()
        self.total_files = total_files
        self.total_bytes = total_bytes or None
        self.done_files = 0
        self.bytes_done = 0
        self._last_reported = 0

    def add_bytes(self, n: int, label: str) -> None:
        report: tuple[int, int] | None = None
        with self._lock:
            self.bytes_done += n
            if self.bytes_done - self._last_reported >= self.REPORT_EVERY:
                self._last_reported = self.bytes_done
                report = (self.done_files, self.bytes_done)
        if report is not None:
            done_files, bytes_done = report
            self._rep.progress(
                done_files, self.total_files, label, bytes_done=bytes_done, bytes_total=self.total_bytes
            )

    def file_done(self, label: str, extra_bytes: int = 0) -> int:
        with self._lock:
            self.done_files += 1
            if extra_bytes:
                self.bytes_done += extra_bytes
            self._last_reported = self.bytes_done
            done_files, bytes_done = self.done_files, self.bytes_done
        self._rep.progress(
            done_files, self.total_files, label, bytes_done=bytes_done, bytes_total=self.total_bytes
        )
        return done_files


class _ThreadSessions:
    """One plain `requests.Session` per worker thread, for `direct` (non-Nexus) downloads.

    Deliberately carries no Nexus `apikey` header.
    """

    def __init__(self):
        self._local = threading.local()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            self._local.session = session
        return session


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
    file_category: str = "",
    direct_url: str | None = None,
    repository: str = "Nexus",
) -> None:
    """Write an MO2-compatible `.meta` sidecar.

    For Nexus mods (the default), `directURL` is omitted and `repository` is "Nexus".
    For `direct` downloads, pass `direct_url` (also used as Wabbajack's `url`/`directURL`
    keys) and `repository=""`.
    """
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # preserve case of keys
    general = {
        "gameName": game_name,
        "modID": str(mod_id),
        "fileID": str(file_id),
        "url": direct_url or "",
        "name": name,
        "description": "",
        "modName": mod_name,
        "version": version,
        "newestVersion": "",
        "fileCategory": file_category,
        "category": "",
        "repository": repository,
        "installed": "false",
        "uninstalled": "false",
        "paused": "false",
        "removed": "false",
    }
    if direct_url:
        # Wabbajack's key for a direct (non-Nexus) download source.
        general["directURL"] = direct_url
    cfg["General"] = general
    with open(meta_path, "w", encoding="utf-8") as fh:
        cfg.write(fh, space_around_delimiters=False)


def _finalize_download(
    tmp_dest: Path,
    dest: Path,
    file_name: str,
    out_dir: Path,
    expected_md5: str | None,
    actual_md5: str,
    entry: DownloadEntry,
) -> None:
    """Shared by the Nexus and direct download paths: MD5-verify `tmp_dest` and either
    rename it to `dest` (ok) or to `<file_name>.md5mismatch` (mismatch), updating `entry`.
    """
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
            return
        entry.status = "md5_mismatch"
        entry.error = f"md5 mismatch: expected {expected_md5}, got {actual_md5}"
        entry.size = size
        entry.path = str(bad_dest.resolve())
        return

    tmp_dest.replace(dest)
    entry.status = "ok"
    entry.size = size


def _download_one(
    clients: _ThreadClients,
    mod: dict[str, Any],
    out_dir: Path,
    domain: str,
    game_name: str,
    tracker: _ByteTracker | None = None,
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
                    if tracker is not None:
                        tracker.add_bytes(len(part), file_name)
    except Exception as e:  # noqa: BLE001 - record any download failure, keep going
        entry.error = f"download failed: {e}"
        try:
            if tmp_dest.exists():
                tmp_dest.unlink()
        except OSError:
            pass
        return entry

    actual_md5 = h.hexdigest()
    _finalize_download(tmp_dest, dest, file_name, out_dir, expected_md5, actual_md5, entry)
    if entry.status == "ok":
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


def _write_meta_for_direct(mod: dict[str, Any], dest: Path, game_name: str, url: str) -> None:
    meta_path = dest.with_name(dest.name + ".meta")
    version = mod.get("version") or ""
    mod_name = mod.get("name") or ""
    _write_meta(
        meta_path,
        game_name=game_name,
        mod_id=0,
        file_id=0,
        name=mod_name,
        mod_name=mod_name,
        version=str(version),
        direct_url=url,
        repository="",
    )


def _direct_file_name(url: str, src: dict[str, Any], mod: dict[str, Any]) -> str | None:
    """URL path basename (URL-decoded); fall back to `logicalFilename`, then the mod's
    `name` if it looks like a file name (has an extension)."""
    path = urlsplit(url).path
    base = unquote(PurePosixPath(path).name) if path else ""
    if base:
        return base
    logical = src.get("logicalFilename")
    if logical:
        return str(logical)
    name = mod.get("name") or ""
    if name and PurePosixPath(name).suffix:
        return name
    return None


def _download_direct(
    sessions: _ThreadSessions,
    mod: dict[str, Any],
    out_dir: Path,
    domain: str,
    game_name: str,
    tracker: _ByteTracker | None = None,
) -> DownloadEntry:
    src = mod.get("source") or {}
    url = src.get("url") or ""
    expected_md5 = src.get("md5")
    entry = DownloadEntry(
        name=mod.get("name") or "",
        mod_id=None,
        file_id=None,
        tag=src.get("tag"),
        md5=expected_md5,
        update_policy=src.get("updatePolicy"),
        install_mode=install_mode(mod),
        optional=bool(mod.get("optional")),
        phase=mod.get("phase", 0),
        mod_type=(mod.get("details") or {}).get("type") or "",
        source_type="direct",
        url=url or None,
    )

    if not url:
        entry.error = "direct source has no url"
        return entry

    file_name = _direct_file_name(url, src, mod)
    if not file_name:
        entry.error = "could not determine a file name for direct download"
        return entry

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
            _write_meta_for_direct(mod, dest, game_name, url)
            return entry

    session = sessions.get()
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    out_dir.mkdir(parents=True, exist_ok=True)

    actual_md5: str | None = None
    last_exc: Exception | None = None
    # 1 initial attempt + DIRECT_MAX_RETRIES retries, 2/4/8s backoff between them.
    for attempt in range(1, DIRECT_MAX_RETRIES + 2):
        try:
            h = hashlib.md5()
            with session.get(
                url, stream=True, timeout=DIRECT_TIMEOUT, allow_redirects=True
            ) as resp:
                resp.raise_for_status()
                with open(tmp_dest, "wb") as fh:
                    for part in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not part:
                            continue
                        fh.write(part)
                        h.update(part)
                        if tracker is not None:
                            tracker.add_bytes(len(part), file_name)
            actual_md5 = h.hexdigest()
            last_exc = None
            break
        except Exception as e:  # noqa: BLE001 - retry, then record and keep going
            last_exc = e
            if attempt <= DIRECT_MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            break

    if last_exc is not None or actual_md5 is None:
        entry.error = f"download failed: {last_exc}"
        try:
            if tmp_dest.exists():
                tmp_dest.unlink()
        except OSError:
            pass
        return entry

    _finalize_download(tmp_dest, dest, file_name, out_dir, expected_md5, actual_md5, entry)
    if entry.status == "ok":
        _write_meta_for_direct(mod, dest, game_name, url)
    return entry


def _unsupported_entry(mod: dict[str, Any]) -> DownloadEntry:
    src = mod.get("source") or {}
    source_type = src.get("type") or "unknown"
    instructions = (src.get("instructions") or mod.get("instructions") or "").strip()
    return DownloadEntry(
        name=mod.get("name") or "",
        mod_id=None,
        file_id=None,
        tag=src.get("tag"),
        md5=src.get("md5"),
        update_policy=src.get("updatePolicy"),
        install_mode=install_mode(mod),
        optional=bool(mod.get("optional")),
        phase=mod.get("phase", 0),
        mod_type=(mod.get("details") or {}).get("type") or "",
        source_type=str(source_type),
        url=src.get("url"),
        status="unsupported",
        error=f"source type {source_type!r} not supported (instructions: {instructions or 'none'})",
    )


def _download_mod(
    clients: _ThreadClients,
    sessions: _ThreadSessions,
    mod: dict[str, Any],
    out_dir: Path,
    domain: str,
    game_name: str,
    tracker: _ByteTracker | None = None,
) -> DownloadEntry:
    source_type = (mod.get("source") or {}).get("type")
    if source_type == "nexus":
        return _download_one(clients, mod, out_dir, domain, game_name, tracker)
    if source_type == "direct":
        return _download_direct(sessions, mod, out_dir, domain, game_name, tracker)
    return _unsupported_entry(mod)


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
    json_path: Path | None = None,
    reporter: Reporter | None = None,
) -> int:
    """Download every mod of `manifest_path` into `out_dir`.

    `json_path` overrides where the run summary is written (default
    `<out_dir>/downloads.json`); `create` uses it to keep the archive store free of
    our own bookkeeping files.
    """
    rep = get_reporter(reporter)
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
        rep.warn("could not determine game domain from manifest")
        return 1

    game_name = mo2_game_name(domain)

    selected_mods = list(mods)
    if not include_optional:
        selected_mods = [m for m in selected_mods if not m.get("optional")]
    if limit is not None:
        selected_mods = selected_mods[:limit]

    total = len(selected_mods)
    if total == 0:
        rep.done("download", "no mods to download")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(manifest=str(manifest_path.resolve()), domain=domain, game_name=game_name)
    clients = _ThreadClients(api_key)
    sessions = _ThreadSessions()

    counts: dict[str, int] = {
        "ok": 0,
        "skipped": 0,
        "md5_mismatch": 0,
        "error": 0,
        "unsupported": 0,
    }
    total_bytes = 0
    start = time.monotonic()
    downloads_json = Path(json_path) if json_path else out_dir / "downloads.json"
    downloads_json.parent.mkdir(parents=True, exist_ok=True)

    rep.stage("download", total)
    rep.log(f"downloading {total} mod(s) for {game_name} ({domain}) with {jobs} worker(s)")

    # `fileSize` comes straight from the manifest -- no extra network round trips --
    # so the rate/ETA line has a real denominator from the first progress() call
    # rather than only after every file's individual size becomes known.
    expected_bytes = sum(int((m.get("source") or {}).get("fileSize") or 0) for m in selected_mods)
    tracker = _ByteTracker(rep, total, expected_bytes)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(
                _download_mod, clients, sessions, mod, out_dir, domain, game_name, tracker
            ): mod
            for mod in selected_mods
        }
        for fut in as_completed(futures):
            mod = futures[fut]
            try:
                entry = fut.result()
            except Exception as e:  # noqa: BLE001 - never let one mod kill the run
                src = mod.get("source") or {}
                source_type = src.get("type") or "unknown"
                mod_id = int(src["modId"]) if source_type == "nexus" and "modId" in src else None
                file_id = (
                    int(src["fileId"]) if source_type == "nexus" and "fileId" in src else None
                )
                entry = DownloadEntry(
                    name=mod.get("name") or "",
                    mod_id=mod_id,
                    file_id=file_id,
                    tag=src.get("tag"),
                    md5=src.get("md5"),
                    update_policy=src.get("updatePolicy"),
                    install_mode=install_mode(mod),
                    optional=bool(mod.get("optional")),
                    phase=mod.get("phase", 0),
                    mod_type=(mod.get("details") or {}).get("type") or "",
                    source_type=str(source_type),
                    url=src.get("url"),
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
                "unsupported": "unsupported",
            }.get(entry.status, entry.status)
            line = f"{status_label:8s} {entry.file_name or entry.name}  {size_str}"
            if entry.error and entry.status != "skipped":
                line += f"  ({entry.error})"
            # Skipped/resumed files never streamed through `tracker.add_bytes` (no
            # chunks were read), so their size is folded in here instead.
            extra_bytes = entry.size if entry.status == "skipped" and entry.size else 0
            done = tracker.file_done(line, extra_bytes=extra_bytes)

            if done % CHECKPOINT_EVERY == 0:
                state.write(downloads_json)

    state.write(downloads_json)
    elapsed = time.monotonic() - start

    rep.done(
        "download",
        f"done in {elapsed:.1f}s: ok={counts.get('ok', 0)} "
        f"skipped={counts.get('skipped', 0)} "
        f"md5_mismatch={counts.get('md5_mismatch', 0)} "
        f"error={counts.get('error', 0)} "
        f"unsupported={counts.get('unsupported', 0)}  total={_fmt_bytes(total_bytes)}",
    )
    rep.log(f"downloads.json: {downloads_json.resolve()}")

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
