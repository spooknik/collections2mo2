"""`c2wj create`: one command from a collection URL to a runnable MO2 instance.

The pipeline stages (`fetch`, `survey`, `download`, `inspect`, `install`, `profile`,
`build`) each remain usable on their own; `create` calls them in order, in-process,
passing explicit paths so nothing depends on the `work/<slug>/<revision>/` layout
those commands default to. The result is a self-contained instance:

    <out>/ModOrganizer.exe, mods/, profiles/, overwrite/, Stock Game/
    <out>/downloads/                 MO2's download folder *and* our archive store
    <out>/c2wj-instance.json         the ledger: who owns which mod folder
    <out>/c2wj/collections/<slug>/<revision>/archive/collection.json
    <out>/c2wj/{downloads,inspect,install,survey,profile-report}.json

Every stage is skipped when its output is already there and consistent with the
stage before it, so an interrupted run resumes and a repeat run is a no-op. The
skip checks live here rather than in the stages: they compare each stage's JSON
against the previous stage's, which only the orchestrator can see.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import archive_inspect, build, installer, ledger, profile, survey
from .downloader import mo2_game_name, run_download
from .manifest import fetch_manifest, load_manifest
from .nexus import AuthRequired, CollectionRef, NexusClient, NexusError
from .reporter import Reporter, get_reporter

# The instance path, the mod folder name (up to 80 chars) and the mod's own nested
# files all share Windows' 260-character budget.
PATH_WARN_LEN = 40

# Files in a reused download store that are ours, not archives.
_NOT_ARCHIVES = {"downloads.json", "inspect.json", "install.json", "survey.json"}


@dataclass
class StageResult:
    name: str
    status: str  # "ok" | "skipped" | "failed" | "warned"
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class Run:
    """Bookkeeping for one `create` invocation."""

    reporter: Reporter
    stages: list[StageResult] = field(default_factory=list)

    def record(self, name: str, status: str, detail: str = "") -> StageResult:
        result = StageResult(name, status, detail)
        self.stages.append(result)
        if status == "skipped":
            self.reporter.done(name, f"up to date ({detail})" if detail else "up to date")
        elif status == "failed":
            self.reporter.warn(f"{name} failed: {detail}")
        return result

    @property
    def failed(self) -> bool:
        return any(s.failed for s in self.stages)


# -- paths ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    out: Path

    @property
    def c2wj(self) -> Path:
        return self.out / "c2wj"

    @property
    def collections(self) -> Path:
        return self.c2wj / "collections"

    @property
    def downloads(self) -> Path:
        return self.out / "downloads"

    @property
    def mods(self) -> Path:
        return self.out / "mods"

    @property
    def downloads_json(self) -> Path:
        return self.c2wj / "downloads.json"

    @property
    def inspect_json(self) -> Path:
        return self.c2wj / "inspect.json"

    @property
    def install_json(self) -> Path:
        return self.c2wj / "install.json"

    @property
    def survey_json(self) -> Path:
        return self.c2wj / "survey.json"

    @property
    def profile_report(self) -> Path:
        return self.c2wj / "profile-report.json"

    @property
    def build_meta(self) -> Path:
        return self.out / "c2wj-build.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _same_path(a: str | None, b: Path) -> bool:
    if not a:
        return False
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


# -- reuse an existing download store -------------------------------------------------


def reuse_downloads(src_dir: Path, dest_dir: Path, rep: Reporter) -> tuple[int, int, int]:
    """Hardlink (same volume) or copy archives + `.meta` sidecars into `dest_dir`.

    Returns `(linked, copied, already_present)`. Anything that is not an archive --
    our own JSON reports, `.part` leftovers, quarantined `.md5mismatch` files -- is
    left behind.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        p
        for p in sorted(src_dir.iterdir())
        if p.is_file()
        and p.name not in _NOT_ARCHIVES
        and not p.name.endswith((".part", ".md5mismatch"))
    ]
    linked = copied = present = 0
    total = len(candidates)
    rep.stage("reuse-downloads", total)
    for i, src in enumerate(candidates, start=1):
        dest = dest_dir / src.name
        if dest.exists():
            present += 1
        else:
            try:
                os.link(src, dest)
                linked += 1
            except OSError:
                # Different volume (or a filesystem without hardlinks): copy instead.
                shutil.copy2(src, dest)
                copied += 1
        rep.progress(i, total, src.name)
    rep.done(
        "reuse-downloads",
        f"{linked} hardlinked, {copied} copied, {present} already present, from {src_dir}",
    )
    return linked, copied, present


# -- per-stage consistency checks ------------------------------------------------------


def downloads_are_current(paths: Paths, manifest_path: Path, mod_count: int) -> bool:
    data = _read_json(paths.downloads_json)
    if not data or not _same_path(data.get("manifest"), manifest_path):
        return False
    entries = data.get("entries") or []
    if len(entries) != mod_count:
        return False
    for entry in entries:
        status = entry.get("status")
        if status == "unsupported":
            continue
        if status not in ("ok", "skipped"):
            return False
        path = entry.get("path")
        if not path or not Path(path).exists():
            return False
    return True


def inspect_is_current(paths: Paths) -> bool:
    downloads = _read_json(paths.downloads_json)
    data = _read_json(paths.inspect_json)
    if not downloads or not data:
        return False
    if not _same_path(data.get("downloads_json"), paths.downloads_json):
        return False
    expected = sum(1 for e in downloads.get("entries") or [] if e.get("status") in ("ok", "skipped"))
    return len(data.get("entries") or []) == expected and expected > 0


def install_is_current(paths: Paths, manifest_path: Path) -> bool:
    inspected = _read_json(paths.inspect_json)
    data = _read_json(paths.install_json)
    if not inspected or not data:
        return False
    if not _same_path(data.get("manifest"), manifest_path):
        return False
    if not _same_path(data.get("mods_dir"), paths.mods):
        return False
    entries = data.get("entries") or []
    if len(entries) != len(inspected.get("entries") or []) or not entries:
        return False
    return all((paths.mods / (e.get("folder") or "")).is_dir() for e in entries)


def profile_is_current(paths: Paths) -> bool:
    installed = _read_json(paths.install_json)
    data = _read_json(paths.profile_report)
    if not installed or not data:
        return False
    if not _same_path(data.get("mo2_dir"), paths.out):
        return False
    if len(data.get("mod_order") or []) != len(installed.get("entries") or []):
        return False
    profile_dir = paths.out / "profiles" / (data.get("profile") or "")
    return (profile_dir / "modlist.txt").exists() and (paths.out / "ModOrganizer.ini").exists()


def build_is_current(paths: Paths, args: argparse.Namespace) -> bool:
    data = _read_json(paths.build_meta)
    if not data or not (paths.out / "ModOrganizer.exe").exists():
        return False
    if not (paths.out / "portable.txt").exists():
        return False
    if data.get("mo2_version") != args.mo2_version:
        return False
    if data.get("rootbuilder_version") != args.rootbuilder_version:
        return False
    if not _same_path(data.get("game_path_source"), Path(args.game_path)):
        return False
    stock = data.get("stock_game_dir")
    if args.stock_game:
        return bool(stock) and Path(stock).is_dir()
    return stock is None


# -- the command ------------------------------------------------------------------------


def _namespace(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def cmd_create(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    rep = get_reporter(reporter)
    run = Run(rep)
    started = time.monotonic()

    paths = Paths(Path(args.out).expanduser().resolve())
    game_path = Path(args.game_path).expanduser().resolve()
    if not game_path.is_dir():
        rep.warn(f"--game-path {game_path} is not a directory")
        return 2

    if len(str(paths.out)) > PATH_WARN_LEN:
        rep.warn(
            f"instance path is {len(str(paths.out))} characters ({paths.out}). Windows caps "
            f"full paths at 260 and mod files nest deeply under it; {PATH_WARN_LEN} or fewer "
            "(e.g. D:\\Skyrim) leaves room for the deepest mod file."
        )

    load_dotenv()
    api_key = os.environ.get("NEXUS_API_KEY") or None
    if not api_key:
        rep.warn("NEXUS_API_KEY is required (set it in .env; see .env.example)")
        return 2

    paths.c2wj.mkdir(parents=True, exist_ok=True)
    paths.downloads.mkdir(parents=True, exist_ok=True)
    paths.mods.mkdir(parents=True, exist_ok=True)

    # -- fetch ---------------------------------------------------------------------
    rep.stage("fetch")
    try:
        ref = CollectionRef.parse(args.url)
        client = NexusClient(api_key=api_key)
        info = client.revision_info(ref, args.revision)
        rev_dir = paths.collections / ref.slug / str(info.revision_number)
        had_manifest = any((rev_dir / "archive").rglob("collection.json")) if rev_dir.exists() else False
        info, manifest_path = fetch_manifest(client, ref, args.revision, paths.collections, info=info)
    except (AuthRequired, NexusError, OSError, ValueError) as exc:
        run.record("fetch", "failed", str(exc))
        return _finish(run, rep, paths, started)

    revision = info.revision_number
    owner = ledger.collection_owner(ref.slug, revision)
    manifest = load_manifest(manifest_path)
    manifest_info = manifest.get("info") or {}
    domain = manifest_info.get("domainName") or info.game
    try:
        game_name = mo2_game_name(domain)
    except NexusError as exc:
        run.record("fetch", "failed", str(exc))
        return _finish(run, rep, paths, started)
    mod_count = len(manifest.get("mods") or [])

    if had_manifest:
        run.record("fetch", "skipped", f"{ref.slug} revision {revision} already fetched")
    else:
        run.record("fetch", "ok", f"{info.name} revision {revision}, {mod_count} mods")
        rep.done("fetch", f"{info.name} [{domain}] revision {revision} -> {manifest_path}")

    # -- reuse an existing archive store -------------------------------------------
    if args.reuse_downloads:
        src = Path(args.reuse_downloads).expanduser().resolve()
        if not src.is_dir():
            rep.warn(f"--reuse-downloads {src} is not a directory, ignored")
        elif src == paths.downloads:
            rep.warn("--reuse-downloads points at the instance's own downloads folder, ignored")
        else:
            linked, copied, present = reuse_downloads(src, paths.downloads, rep)
            run.record("reuse-downloads", "ok", f"{linked} linked, {copied} copied, {present} kept")

    # -- survey (optional pre-flight) ----------------------------------------------
    if args.skip_survey:
        run.record("survey", "skipped", "--skip-survey")
    elif _survey_is_current(paths.survey_json, manifest_path):
        run.record("survey", "skipped", str(paths.survey_json))
    else:
        try:
            rc = survey.run_survey(
                manifest_path=manifest_path,
                out_path=paths.survey_json,
                jobs=args.jobs,
                survey_all=False,
                min_remaining=100,
                limit=None,
                api_key=api_key,
                reporter=rep,
            )
        except (AuthRequired, NexusError) as exc:
            rc, exc_text = 1, str(exc)
        else:
            exc_text = ""
        if rc == 0:
            run.record("survey", "ok")
        elif rc == 3:
            # Nexus's hourly v1 budget ran out. The survey only informs the operator;
            # nothing downstream needs it, so carry on.
            run.record("survey", "warned", "stopped early: Nexus hourly rate limit")
            rep.warn("survey stopped early (Nexus hourly rate limit); continuing without it")
        else:
            run.record("survey", "warned", exc_text or f"exit {rc}")
            rep.warn(f"survey did not complete ({exc_text or f'exit {rc}'}); continuing without it")

    # -- download -------------------------------------------------------------------
    if downloads_are_current(paths, manifest_path, mod_count):
        run.record("download", "skipped", f"{mod_count} archives present in {paths.downloads}")
    else:
        try:
            rc = run_download(
                manifest_path=manifest_path,
                out_dir=paths.downloads,
                jobs=args.jobs,
                limit=None,
                include_optional=True,
                api_key=api_key,
                json_path=paths.downloads_json,
                reporter=rep,
            )
        except (AuthRequired, NexusError) as exc:
            run.record("download", "failed", str(exc))
            return _finish(run, rep, paths, started)
        if rc != 0:
            run.record("download", "failed", "one or more archives failed or mismatched")
            return _finish(run, rep, paths, started)
        run.record("download", "ok")

    # -- inspect --------------------------------------------------------------------
    if inspect_is_current(paths):
        run.record("inspect", "skipped", str(paths.inspect_json))
    else:
        rc = archive_inspect.cmd_inspect(
            _namespace(
                downloads_json=str(paths.downloads_json),
                out=str(paths.inspect_json),
                jobs=args.jobs,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("inspect", "failed", "one or more archives could not be listed")
            return _finish(run, rep, paths, started)
        run.record("inspect", "ok")

    # -- install --------------------------------------------------------------------
    if install_is_current(paths, manifest_path):
        run.record("install", "skipped", f"mods/ matches {paths.install_json.name}")
    else:
        rc = installer.cmd_install(
            _namespace(
                inspect_json=str(paths.inspect_json),
                manifest=str(manifest_path),
                mods_dir=str(paths.mods),
                only=None,
                game_path=str(game_path),
                jobs=args.jobs,
                force=False,
                choices_overrides=args.choices_overrides,
                out=str(paths.install_json),
                owner=owner,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("install", "failed", "one or more mods failed to install")
            return _finish(run, rep, paths, started)
        run.record("install", "ok")

    # -- profile --------------------------------------------------------------------
    profile_skipped = profile_is_current(paths)
    if profile_skipped:
        run.record("profile", "skipped", str(paths.profile_report))
    else:
        rc = profile.cmd_profile(
            _namespace(
                install_json=str(paths.install_json),
                mo2_dir=str(paths.out),
                profile_name=None,
                game_path=str(game_path),
                separators=True,
                mo2_version=args.mo2_version,
                disable_optional=False,
                resolution=args.resolution,
                vsync=args.vsync,
                window=args.window,
                keep_inis=False,
                report_out=str(paths.profile_report),
                owner=owner,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("profile", "failed", "profile writer returned a non-zero status")
            return _finish(run, rep, paths, started)
        run.record("profile", "ok")

    # -- build ----------------------------------------------------------------------
    # `profile` rewrites ModOrganizer.ini's gamePath to the *source* game folder, so
    # build has to run again to re-point it at the Stock Game copy whenever profile ran.
    if profile_skipped and build_is_current(paths, args):
        run.record("build", "skipped", "MO2, Root Builder and ModOrganizer.ini in place")
    else:
        rc = build.cmd_build(
            _namespace(
                mo2_dir=str(paths.out),
                game_path=str(game_path),
                mo2_version=args.mo2_version,
                rootbuilder_version=args.rootbuilder_version,
                force=False,
                stock_game=args.stock_game,
                stock_game_dir=None,
                force_stock=False,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("build", "failed", "MO2 / Root Builder installation returned non-zero")
            return _finish(run, rep, paths, started)
        run.record("build", "ok")

    # -- ledger ---------------------------------------------------------------------
    rep.stage("ledger")
    build_meta = _read_json(paths.build_meta) or {}
    install_data = _read_json(paths.install_json) or {}
    report = _read_json(paths.profile_report) or {}

    led = ledger.load(paths.out)
    led.set_game(
        domain=domain,
        mo2_name=game_name,
        source_path=str(game_path),
        stock_game_dir=build_meta.get("stock_game_dir"),
    )
    led.set_mo2(version=args.mo2_version, rootbuilder_version=args.rootbuilder_version)
    led.register_layer(
        ref.slug,
        revision,
        name=manifest_info.get("name") or info.name,
        author=manifest_info.get("author") or "",
        profile=report.get("profile") or "",
        separators=report.get("separators") or [],
        manifest=manifest_path.relative_to(paths.out).as_posix(),
    )
    for entry in install_data.get("entries") or []:
        folder = entry.get("folder")
        if not folder:
            continue
        led.set_mod_owner(
            folder,
            owner,
            tag=entry.get("tag") or "",
            md5=entry.get("md5") or "",
            install_mode=entry.get("install_mode") or "",
            strategy=entry.get("strategy") or "",
            plugins=entry.get("plugins") or [],
        )
    led.save()

    owned = len(led.mods_owned_by(owner))
    user_mods = [f for f, owners in led.scan_mods_dir(paths.mods).items() if owners == ["user"]]
    detail = f"{owned} mods owned by {owner}"
    if user_mods:
        detail += f", {len(user_mods)} user mod(s)"
    run.record("ledger", "ok", detail)
    rep.done("ledger", f"{detail} -> {led.path}")
    if user_mods:
        rep.log("mods not owned by any layer (treated as user mods):")
        for folder in user_mods[:20]:
            rep.log(f"  {folder}")
        if len(user_mods) > 20:
            rep.log(f"  ... +{len(user_mods) - 20} more")

    return _finish(run, rep, paths, started)


def _survey_is_current(survey_json: Path, manifest_path: Path) -> bool:
    data = _read_json(survey_json)
    if not data:
        return False
    return _same_path(data.get("manifest"), manifest_path) and bool(data.get("entries"))


def _finish(run: Run, rep: Reporter, paths: Paths, started: float) -> int:
    elapsed = time.monotonic() - started
    rep.stage("summary")
    for stage in run.stages:
        line = f"  {stage.status:<8} {stage.name}"
        if stage.detail:
            line += f"  ({stage.detail})"
        rep.log(line)
    rep.log(f"  elapsed: {elapsed:.1f}s")
    if run.failed:
        rep.warn("create did not finish: see the failed stage above")
        return 1
    rep.log("")
    rep.log(f"launch MO2:  {paths.out / 'ModOrganizer.exe'}")
    rep.log(f"ledger:      {paths.out / ledger.LEDGER_NAME}")
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "create",
        help="one command: collection URL -> a runnable, self-contained MO2 instance",
    )
    p.add_argument("url", help="collection URL")
    p.add_argument("--out", required=True, help="instance directory to create (keep it short)")
    p.add_argument("--game-path", required=True, help="the game install to build against")
    p.add_argument("--revision", type=int, default=None, help="revision number (default: latest)")
    p.add_argument(
        "--stock-game",
        action="store_true",
        default=False,
        help="copy the game into the instance and point MO2 at the copy (Wabbajack's "
        "'Stock Game' convention), so nothing ever patches the real install",
    )
    p.add_argument(
        "--reuse-downloads",
        default=None,
        help="an existing download store to hardlink/copy archives + .meta from first",
    )
    p.add_argument("--jobs", type=int, default=4, help="parallel workers per stage (default: 4)")
    p.add_argument(
        "--resolution",
        type=profile._parse_resolution_arg,
        default="keep",
        help="profile display resolution: 'auto', 'keep', or WxH (default: keep)",
    )
    p.add_argument(
        "--vsync",
        choices=["on", "off", "keep"],
        default="keep",
        help="profile display vsync (default: keep)",
    )
    p.add_argument(
        "--window",
        choices=["fullscreen", "borderless", "windowed", "keep"],
        default="keep",
        help="profile window mode (default: keep)",
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
        "--mo2-version",
        default=build.DEFAULT_MO2_VERSION,
        help=f"Mod Organizer 2 release to install (default: {build.DEFAULT_MO2_VERSION})",
    )
    p.add_argument(
        "--rootbuilder-version",
        default=build.DEFAULT_ROOTBUILDER_VERSION,
        help=f"Root Builder release to install (default: {build.DEFAULT_ROOTBUILDER_VERSION})",
    )
    p.set_defaults(func=cmd_create)
