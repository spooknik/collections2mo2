"""Survey Nexus file-content previews for mods without a recorded FOMOD install.

Answers, without downloading any archive: "which mods WITHOUT recorded FOMOD
`choices` nevertheless ship a FOMOD installer?" Nexus exposes a per-file content
listing via two calls:

1. `NexusClient.mod_file_info` (v1 REST, rate-limited: `x-rl-hourly-remaining` /
   `x-rl-hourly-limit`, ~2000/h) returns `content_preview_link`.
2. That link is an unauthenticated, non-rate-limited S3 URL returning
   `{"children": [{"name", "path", "type": "file"|"directory", "size"?, "children"?}, ...]}`
   (observed 2026-09; `size` on file nodes is a human string like "19.8 MB", not usable
   for byte totals). We flatten this tree to relative paths and classify layout the same
   way `archive_inspect.py` classifies real archive listings.

Because a full survey can burn most of an hour's v1 budget, results are cached in
`survey.json` keyed by `source.tag`, written incrementally, and a run stops cleanly
(exit 3) when the hourly budget gets low.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import requests

from .manifest import install_mode, load_manifest
from .nexus import API_BASE, AuthRequired, NexusClient, NexusError
from .reporter import Reporter, get_reporter

CHECKPOINT_EVERY = 25
RESYNC_EVERY = 50

# -- Minimal layout classifier -------------------------------------------------
# Mirrors archive_inspect.py's `_classify_layout` (same labels, same DATA_DIRS idea),
# but operates on (path, is_dir) tuples flattened from a preview JSON tree instead of
# ArchiveEntry objects listed from a real archive (inspect_archive only works on
# downloaded files, so it can't be reused directly).

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


def _find_fomod_dir(files: list[str]) -> str | None:
    for p in files:
        parts = p.split("/")
        if len(parts) >= 2 and parts[-1].lower() == "moduleconfig.xml" and parts[-2].lower() == "fomod":
            return "/".join(parts[:-1])
    return None


def _is_top_folder(name: str, entries: list[tuple[str, bool]]) -> bool:
    prefix = name + "/"
    for path, is_dir in entries:
        if path == name and is_dir:
            return True
        if path.startswith(prefix):
            return True
    return False


def _classify_layout(
    top_level: list[str], entries: list[tuple[str, bool]], fomod_dir: str | None
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
    if len(considered) == 1 and considered[0].lower() == "data" and _is_top_folder(considered[0], entries):
        return "data_wrapped"
    if len(considered) == 1 and considered[0].lower() != "data" and _is_top_folder(considered[0], entries):
        return "single_folder"
    if any(_ext(n) in _ROOT_EXTS for n in considered):
        return "root"
    if fomod_dir is not None:
        return "fomod_only"
    return "unknown"


def _flatten(node: dict[str, Any], out: list[tuple[str, bool]]) -> None:
    """Flatten one preview-tree node (and its children) into (relative_path, is_dir) pairs."""
    path = str(node.get("path") or node.get("name") or "").replace("\\", "/")
    is_dir = node.get("type") != "file"
    if path:
        out.append((path, is_dir))
    children = node.get("children")
    if children:
        for child in children:
            _flatten(child, out)


# -- Survey state ---------------------------------------------------------------


@dataclass
class SurveyEntry:
    name: str = ""
    mod_id: int = 0
    file_id: int = 0
    md5: str | None = None
    install_mode: str = "fresh"
    optional: bool = False
    phase: Any = 0
    mod_type: str = ""
    file_name: str | None = None
    size: int | None = None
    category_id: int | None = None
    preview_link: str | None = None
    preview_fetched: bool = False
    has_fomod: bool | None = None
    fomod_dir: str | None = None
    top_level: list[str] | None = None
    file_count: int | None = None
    plugins: list[str] | None = None
    archive_type: str | None = None
    layout: str | None = None
    error: str | None = None


_ENTRY_FIELDS = {f.name for f in fields(SurveyEntry)}


@dataclass
class SurveyState:
    manifest: str
    domain: str
    entries: dict[str, SurveyEntry] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "domain": self.domain,
            "entries": {tag: asdict(e) for tag, e in self.entries.items()},
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SurveyState | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entries: dict[str, SurveyEntry] = {}
        for tag, raw in (data.get("entries") or {}).items():
            filtered = {k: v for k, v in raw.items() if k in _ENTRY_FIELDS}
            entries[tag] = SurveyEntry(**filtered)
        return cls(
            manifest=data.get("manifest", ""),
            domain=data.get("domain", ""),
            entries=entries,
        )


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


class RateLimiter:
    """Tracks the Nexus v1 hourly rate limit via periodic `validate.json` checks.

    `mod_file_info` (nexus.py's `_get_v1_json`) only returns parsed JSON, not headers,
    and we must not modify nexus.py -- so headers are read by re-requesting the cheap
    `validate.json` endpoint directly through `client.session` every `resync_every`
    calls, and once up front.
    """

    def __init__(self, min_remaining: int, resync_every: int = RESYNC_EVERY):
        self.min_remaining = min_remaining
        self.resync_every = resync_every
        self._lock = threading.Lock()
        self.remaining: int | None = None
        self.limit: int | None = None
        self.daily_remaining: int | None = None
        self.hourly_reset: str | None = None
        self._calls_since_sync = 0

    def sync(self, client: NexusClient) -> None:
        resp = client.session.get(f"{API_BASE}/v1/users/validate.json", timeout=30)
        if resp.status_code in (401, 403):
            raise AuthRequired(
                f"Nexus rejected the API key ({resp.status_code}) while checking rate limits"
            )
        resp.raise_for_status()
        with self._lock:
            if resp.headers.get("x-rl-hourly-remaining") is not None:
                self.remaining = int(resp.headers["x-rl-hourly-remaining"])
            if resp.headers.get("x-rl-hourly-limit") is not None:
                self.limit = int(resp.headers["x-rl-hourly-limit"])
            if resp.headers.get("x-rl-daily-remaining") is not None:
                self.daily_remaining = int(resp.headers["x-rl-daily-remaining"])
            self.hourly_reset = resp.headers.get("x-rl-hourly-reset") or self.hourly_reset
            self._calls_since_sync = 0

    def record_call(self, client: NexusClient) -> None:
        """Called after every v1 request; periodically re-syncs against real headers."""
        need_sync = False
        with self._lock:
            if self.remaining is not None:
                self.remaining -= 1
            self._calls_since_sync += 1
            if self._calls_since_sync >= self.resync_every:
                self._calls_since_sync = 0
                need_sync = True
        if need_sync:
            self.sync(client)

    def should_stop(self) -> bool:
        with self._lock:
            return self.remaining is not None and self.remaining < self.min_remaining


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fetch_preview(url: str) -> dict[str, Any]:
    """GET a content-preview JSON tree. Retries twice (3 attempts total) on connection error."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise NexusError(f"preview fetch connection error: {e}") from e
        if resp.status_code != 200:
            raise NexusError(f"preview fetch HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise NexusError(f"preview fetch returned non-JSON: {e}") from e
    raise NexusError(f"preview fetch failed: {last_exc}")


def _survey_one(
    clients: _ThreadClients,
    mod: dict[str, Any],
    domain: str,
    limiter: RateLimiter,
) -> tuple[str, SurveyEntry]:
    src = mod.get("source") or {}
    mod_id = int(src.get("modId") or 0)
    file_id = int(src.get("fileId") or 0)
    tag = src.get("tag") or f"{mod_id}_{file_id}"

    entry = SurveyEntry(
        name=mod.get("name") or "",
        mod_id=mod_id,
        file_id=file_id,
        md5=src.get("md5"),
        install_mode=install_mode(mod),
        optional=bool(mod.get("optional")),
        phase=mod.get("phase", 0),
        mod_type=(mod.get("details") or {}).get("type") or "",
    )

    client = clients.get()
    try:
        info = client.mod_file_info(domain, mod_id, file_id)
    except AuthRequired:
        raise
    except (NexusError, KeyError, ValueError) as e:
        entry.error = f"file info failed: {e}"
        return tag, entry
    finally:
        limiter.record_call(client)

    entry.file_name = info.get("file_name")
    entry.size = info.get("size_in_bytes")
    entry.category_id = info.get("category_id")
    preview_link = info.get("content_preview_link")
    entry.preview_link = preview_link

    if not preview_link:
        entry.error = "no content_preview_link in file info"
        return tag, entry

    try:
        tree = _fetch_preview(preview_link)
    except NexusError as e:
        entry.error = str(e)
        return tag, entry

    flattened: list[tuple[str, bool]] = []
    for child in tree.get("children") or []:
        _flatten(child, flattened)
    files = [p for p, is_dir in flattened if not is_dir]

    fomod_dir = _find_fomod_dir(files)
    top_level = sorted({p.split("/", 1)[0] for p, _ in flattened if p})
    layout = _classify_layout(top_level, flattened, fomod_dir)
    plugins = sorted({p.rsplit("/", 1)[-1] for p in files if _ext(p) in _PLUGIN_EXTS})
    archive_type = _ext(entry.file_name or "").lstrip(".")

    entry.has_fomod = fomod_dir is not None
    entry.fomod_dir = fomod_dir
    entry.top_level = top_level
    entry.file_count = len(files)
    entry.plugins = plugins
    entry.archive_type = archive_type
    entry.layout = layout
    entry.preview_fetched = True
    entry.error = None
    return tag, entry


# -- Run orchestration ------------------------------------------------------------


def _select_targets(mods: list[dict[str, Any]], survey_all: bool) -> list[dict[str, Any]]:
    nexus_mods = [m for m in mods if (m.get("source") or {}).get("type") == "nexus"]
    if survey_all:
        return nexus_mods
    return [m for m in nexus_mods if install_mode(m) == "fresh"]


def _print_report(
    state: SurveyState,
    targets: list[dict[str, Any]],
    all_mods: list[dict[str, Any]],
    surveyed_this_run: int,
    remaining_this_run: int,
) -> None:
    target_tags = {(m.get("source") or {}).get("tag") for m in targets}
    fetched = [e for tag, e in state.entries.items() if tag in target_tags and e.preview_fetched]
    errored = [
        e for tag, e in state.entries.items() if tag in target_tags and not e.preview_fetched
    ]

    print(f"\n== survey coverage: {len(fetched)}/{len(targets)} targets fetched")
    if errored:
        print(f"   {len(errored)} target(s) not yet fetched (errors or not attempted)")
        for e in errored[:20]:
            if e.error:
                print(f"     - {e.name}: {e.error}")
        if len(errored) > 20:
            print(f"     ... and {len(errored) - 20} more")

    fresh_fomod = [e for e in fetched if e.install_mode == "fresh" and e.has_fomod]
    print(f"\n== fresh-mode mods WITH a FOMOD installer -- the headline ({len(fresh_fomod)})")
    for e in fresh_fomod:
        print(f"  - {e.name}")

    layouts = Counter(e.layout for e in fetched)
    print("\n== layouts")
    for k, v in sorted(layouts.items(), key=lambda kv: (kv[0] or "")):
        print(f"  {k}: {v}")

    archive_types = Counter(e.archive_type for e in fetched)
    print("\n== archive types")
    for k, v in sorted(archive_types.items(), key=lambda kv: (kv[0] or "")):
        print(f"  {k or '(none)'}: {v}")

    total_size = sum(e.size or 0 for e in fetched)
    print(f"\n== total size of surveyed files: {_fmt_bytes(total_size)}")

    with_choices = sum(1 for m in all_mods if install_mode(m) == "choices")
    print(f"\n== mods with recorded FOMOD `choices` (context): {with_choices}")

    print(f"\n== this run: surveyed {surveyed_this_run} new, {remaining_this_run} left to try")


def run_survey(
    manifest_path: Path,
    out_path: Path,
    jobs: int,
    survey_all: bool,
    min_remaining: int,
    limit: int | None,
    api_key: str | None,
    reporter: Reporter | None = None,
) -> int:
    rep = get_reporter(reporter)
    manifest = load_manifest(manifest_path)
    info = manifest.get("info") or {}
    domain = info.get("domainName")
    all_mods: list[dict[str, Any]] = manifest.get("mods") or []
    if not domain:
        for m in all_mods:
            d = m.get("domainName")
            if d:
                domain = d
                break
    if not domain:
        rep.warn("could not determine game domain from manifest")
        return 1

    rep.stage("survey")
    targets = _select_targets(all_mods, survey_all)
    if limit is not None:
        targets = targets[:limit]

    state = SurveyState.load(out_path) or SurveyState(
        manifest=str(manifest_path.resolve()), domain=domain, entries={}
    )
    state.manifest = str(manifest_path.resolve())
    state.domain = domain

    to_process = []
    for m in targets:
        tag = (m.get("source") or {}).get("tag") or ""
        existing = state.entries.get(tag)
        if existing is not None and existing.preview_fetched:
            continue
        to_process.append(m)

    if not to_process:
        rep.done("survey", f"all {len(targets)} target(s) already surveyed (cached in {out_path})")
        _print_report(state, targets, all_mods, 0, 0)
        return 0

    clients = _ThreadClients(api_key)
    limiter = RateLimiter(min_remaining)
    try:
        limiter.sync(clients.get())
    except AuthRequired as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (NexusError, requests.RequestException) as e:
        print(f"warning: could not read initial rate-limit headers: {e}", file=sys.stderr)

    rep.log(
        f"surveying {len(to_process)} of {len(targets)} target(s) for {domain} "
        f"with {jobs} worker(s) (hourly remaining: {limiter.remaining}/{limiter.limit})"
    )

    processed = 0
    since_write = 0
    stopped_for_rate_limit = False
    fatal_error: str | None = None
    target_tags = {(m.get("source") or {}).get("tag") or "" for m in targets}

    def _preview_failure_stats() -> tuple[int, int]:
        """Cumulative (attempts, failures) across ALL surveyed targets (cache + this run).

        Using the full cached state (not just this process's counters) avoids a false
        "stop" trigger from an unlucky small run-local sample -- a normal minority of
        Nexus files (older uploads) simply never got a preview generated and 404 on
        the content-preview endpoint; that is not evidence of a systemic outage.
        """
        attempts = failures = 0
        for tag, e in state.entries.items():
            if tag not in target_tags or not e.preview_link:
                continue
            attempts += 1
            if not e.preview_fetched:
                failures += 1
        return attempts, failures

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        idx = 0
        n = len(to_process)
        while idx < n:
            if limiter.should_stop():
                stopped_for_rate_limit = True
                break
            batch = to_process[idx : idx + jobs]
            idx += jobs
            futures = {pool.submit(_survey_one, clients, mod, domain, limiter): mod for mod in batch}
            try:
                for fut in as_completed(futures):
                    tag, entry = fut.result()
                    state.entries[tag] = entry
                    processed += 1
                    since_write += 1
                    if since_write >= CHECKPOINT_EVERY:
                        state.write(out_path)
                        since_write = 0
            except AuthRequired as e:
                fatal_error = str(e)
                break

            attempts, failures = _preview_failure_stats()
            if not fatal_error and attempts >= 100 and failures / attempts > 0.6:
                fatal_error = (
                    f"{failures}/{attempts} preview fetches failed (non-JSON / 403 / 404) "
                    "-- stopping, investigate before continuing"
                )
                break

    state.write(out_path)

    if fatal_error:
        rep.warn(fatal_error)
        _print_report(state, targets, all_mods, processed, len(to_process) - processed)
        return 1

    remaining = len(to_process) - processed
    if stopped_for_rate_limit:
        print(
            f"\nstopping: hourly remaining ({limiter.remaining}) is below --min-remaining "
            f"({min_remaining}). {remaining} mod(s) left to survey."
        )
        reset_msg = f" (resets at {limiter.hourly_reset})" if limiter.hourly_reset else ""
        print(f"resume after the hourly window resets{reset_msg} by re-running the same command.")

    _print_report(state, targets, all_mods, processed, remaining)

    if remaining == 0:
        rep.done("survey", f"{len(targets)} target(s) surveyed")
        return 0
    return 3 if stopped_for_rate_limit else 1


def cmd_survey(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = manifest_path.resolve().parent.parent / "survey.json"

    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("NEXUS_API_KEY") or None
    if not api_key:
        print(
            "error: NEXUS_API_KEY is required (set it in .env). See .env.example.",
            file=sys.stderr,
        )
        return 2

    try:
        return run_survey(
            manifest_path=manifest_path,
            out_path=out_path,
            jobs=args.jobs,
            survey_all=args.all,
            min_remaining=args.min_remaining,
            limit=args.limit,
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
        "survey",
        help="survey Nexus file-content previews for mods without recorded FOMOD choices",
    )
    p.add_argument("manifest", help="path to collection.json")
    p.add_argument(
        "--out",
        default=None,
        help="output path (default: <manifest dir>/../survey.json)",
    )
    p.add_argument("--jobs", type=int, default=4, help="parallel survey workers (default: 4)")
    p.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="survey every Nexus mod (default: only mods without choices/hashes)",
    )
    p.add_argument(
        "--min-remaining",
        dest="min_remaining",
        type=int,
        default=100,
        help="stop issuing v1 calls when hourly remaining drops below this (default: 100)",
    )
    p.add_argument("--limit", type=int, default=None, help="only consider the first N targets")
    p.set_defaults(func=cmd_survey)
