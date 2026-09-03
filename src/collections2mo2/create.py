"""`c2mo2 create`: one command from a collection URL to a runnable MO2 instance.

The pipeline stages (`fetch`, `survey`, `download`, `inspect`, `install`, `profile`,
`build`) each remain usable on their own; `create` calls them in order, in-process,
passing explicit paths so nothing depends on the `work/<slug>/<revision>/` layout
those commands default to. The result is a self-contained instance:

    <out>/ModOrganizer.exe, mods/, profiles/, overwrite/, Stock Game/
    <out>/downloads/                 MO2's download folder *and* our archive store
    <out>/c2mo2-instance.json         the ledger: who owns which mod folder
    <out>/c2mo2/collections/<slug>/<revision>/archive/collection.json
    <out>/c2mo2/<slug>-<rev>.{downloads,inspect,install,survey}.json
    <out>/c2mo2/profile-report.json   the profile, rendered from *all* layers at once

Every stage is skipped when its output is already there and consistent with the
stage before it, so an interrupted run resumes and a repeat run is a no-op. The
skip checks live here rather than in the stages: they compare each stage's JSON
against the previous stage's, which only the orchestrator can see.

`create` is "initialise an instance, add its first collection layer, build it".
`c2mo2 add` (see `layers.py`) runs the same `add_layer` against an instance that
already exists, which is why the stage JSON is named per layer: a second
collection must not overwrite the first one's record of what it downloaded and
installed. Instances written before layering carry plain `downloads.json` /
`inspect.json` / `install.json`; `migrate_instance` renames those onto the base
layer's names the first time one of the commands touches such an instance.
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
from .reporter import Reporter, get_reporter, stdout_to_reporter

# The instance path, the mod folder name (up to 80 chars) and the mod's own nested
# files all share Windows' 260-character budget.
PATH_WARN_LEN = 40

# The stage JSON a layer owns, as `LayerPaths attribute -> pre-layering file name`.
STAGE_FILES = {
    "downloads_json": "downloads.json",
    "inspect_json": "inspect.json",
    "install_json": "install.json",
    "survey_json": "survey.json",
}


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
    """Bookkeeping for one `create` / `add` invocation."""

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
    """The instance-wide folders. Anything per-collection lives in `LayerPaths`."""

    out: Path

    @property
    def stage(self) -> Path:
        return self.out / "c2mo2"

    @property
    def collections(self) -> Path:
        return self.stage / "collections"

    @property
    def downloads(self) -> Path:
        return self.out / "downloads"

    @property
    def mods(self) -> Path:
        return self.out / "mods"

    @property
    def profile_report(self) -> Path:
        return self.stage / "profile-report.json"

    @property
    def build_meta(self) -> Path:
        return self.out / "c2mo2-build.json"


@dataclass(frozen=True)
class LayerPaths:
    """One collection layer's stage JSON, namespaced by `<slug>-<revision>`.

    Layers share `mods/` and `downloads/` -- that is the point, one MO2 mods tree and
    one archive store -- but each keeps its own record of what it downloaded, inspected
    and installed, so adding a second collection cannot erase the first one's.
    """

    paths: Paths
    slug: str
    revision: int | str

    @property
    def prefix(self) -> str:
        return f"{self.slug}-{self.revision}"

    @property
    def out(self) -> Path:
        return self.paths.out

    @property
    def mods(self) -> Path:
        return self.paths.mods

    @property
    def downloads(self) -> Path:
        return self.paths.downloads

    @property
    def downloads_json(self) -> Path:
        return self.paths.stage / f"{self.prefix}.downloads.json"

    @property
    def inspect_json(self) -> Path:
        return self.paths.stage / f"{self.prefix}.inspect.json"

    @property
    def install_json(self) -> Path:
        return self.paths.stage / f"{self.prefix}.install.json"

    @property
    def survey_json(self) -> Path:
        return self.paths.stage / f"{self.prefix}.survey.json"

    def ledger_files(self) -> dict[str, str]:
        """The layer's stage JSON as instance-relative paths, for the ledger."""
        return {
            key.removesuffix("_json"): getattr(self, key)
            .resolve()
            .relative_to(self.paths.out.resolve())
            .as_posix()
            for key in STAGE_FILES
        }


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


def migrate_instance(paths: Paths, led: ledger.Ledger, rep: Reporter) -> list[str]:
    """Move a pre-layering instance's stage JSON onto its base layer's per-layer names.

    Renames `c2mo2/downloads.json` to `c2mo2/<slug>-<rev>.downloads.json` and friends,
    repoints `inspect.json`'s reference to the `downloads.json` it came from, and fills
    in the base layer's `files` record. Returns the renames performed, empty for an
    instance that is already current.
    """
    layers = led.data.get("layers") or []
    if not layers:
        return []
    base = layers[0]
    lp = LayerPaths(paths, base.get("slug") or "", base.get("revision"))
    moved: list[str] = []
    for key, legacy_name in STAGE_FILES.items():
        legacy = paths.stage / legacy_name
        current = getattr(lp, key)
        if legacy.exists() and not current.exists():
            legacy.rename(current)
            moved.append(f"{legacy_name} -> {current.name}")
    if moved:
        data = _read_json(lp.inspect_json)
        if data and data.get("downloads_json"):
            data["downloads_json"] = str(lp.downloads_json.resolve())
            lp.inspect_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        rep.log(f"migrated pre-layering stage files: {', '.join(moved)}")
    if not base.get("files"):
        base["files"] = lp.ledger_files()
    return moved


# -- pre-rename instances (`collections2wabbajack` / `c2wj`) --------------------------

LEGACY_STAGE_DIR_NAME = "c2wj"
LEGACY_BUILD_META_NAME = "c2wj-build.json"


def migrate_legacy_instance(paths: Paths, rep: Reporter) -> list[str]:
    """Rename a pre-`collections2mo2` instance's files onto their current names.

    The project used to be called `collections2wabbajack` (CLI `c2wj`) and stamped that
    name into the instance itself: the ledger `c2wj-instance.json`, the stage folder
    `c2wj/`, the build record `c2wj-build.json`, and the generated display-override mod
    `c2wj Display Settings` (folder, ledger entry and `modlist.txt` rows). Each is
    renamed only when the current name is absent and the legacy one present, so this is
    idempotent, safe to call on every open, and never clobbers a current file.

    Runs before anything reads the instance -- see the call sites in `cmd_create`,
    `layers.cmd_add` / `cmd_remove`, `update.cmd_update` / `cmd_status`, `tools`,
    `wabbajack.cmd_wabbajack`, `profile.cmd_profile_instance` and `api.load_instance`.
    Returns the renames performed, empty for an instance that is already current.
    """
    renamed: list[str] = []

    def rename(legacy: Path, current: Path) -> None:
        if not legacy.exists() or current.exists():
            return
        legacy.rename(current)
        renamed.append(f"{legacy.name} -> {current.name}")
        rep.log(f"migrated legacy name: {legacy.name} -> {current.name}")

    rename(paths.out / ledger.LEGACY_LEDGER_NAME, paths.out / ledger.LEDGER_NAME)
    rename(paths.out / LEGACY_STAGE_DIR_NAME, paths.stage)
    rename(paths.out / LEGACY_BUILD_META_NAME, paths.build_meta)
    renamed.extend(_migrate_legacy_display_mod(paths, rep))
    return renamed


def _migrate_legacy_display_mod(paths: Paths, rep: Reporter) -> list[str]:
    """Rename the generated `c2wj Display Settings` mod, in the ledger and on disk.

    Only when the ledger still marks it as ours (`generated_by` is the legacy marker) --
    a folder of that name the user has repurposed is left alone. The ledger entry is
    rekeyed in place (keeping its position in `mods`) and every `profiles/*/modlist.txt`
    row for it is rewritten, `+`/`-` prefix preserved.
    """
    old_name = profile.LEGACY_DISPLAY_OVERRIDE_MOD_NAME
    new_name = profile.DISPLAY_OVERRIDE_MOD_NAME
    led = ledger.load(paths.out)
    mods = led.data.get("mods")
    if not isinstance(mods, dict):
        return []
    record = mods.get(old_name)
    if not isinstance(record, dict):
        return []
    if record.get("generated_by") != profile.LEGACY_DISPLAY_OVERRIDE_MARKER:
        return []
    if new_name in mods:
        return []

    old_dir = paths.mods / old_name
    new_dir = paths.mods / new_name
    if old_dir.is_dir() and not new_dir.exists():
        old_dir.rename(new_dir)
    # `apply_sse_display_tweaks_override` only writes meta.ini when it is missing, so the
    # legacy "Generated by c2wj" note would otherwise outlive the rename.
    meta = new_dir / "meta.ini"
    if meta.is_file():
        try:
            text = meta.read_text(encoding="utf-8")
        except OSError:
            text = None
        if text and profile.LEGACY_DISPLAY_OVERRIDE_MARKER in text:
            text = text.replace("Generated by c2wj (", "Generated by c2mo2 (").replace(
                profile.LEGACY_DISPLAY_OVERRIDE_MARKER, profile.DISPLAY_OVERRIDE_MARKER
            )
            meta.write_text(text, encoding="utf-8")

    record["generated_by"] = profile.DISPLAY_OVERRIDE_MARKER
    # Rekey in place: rebuild `mods` so the entry keeps its position.
    led.data["mods"] = {(new_name if k == old_name else k): v for k, v in mods.items()}
    led.save()

    for modlist in sorted(paths.out.glob("profiles/*/modlist.txt")):
        try:
            text = modlist.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if body[:1] in ("+", "-", "*") and body[1:] == old_name:
                lines[i] = body[:1] + new_name + line[len(body) :]
                changed = True
        if changed:
            modlist.write_text("".join(lines), encoding="utf-8")

    rep.log(f"migrated legacy name: mod {old_name!r} -> {new_name!r}")
    return [f"{old_name} -> {new_name}"]


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
        if p.is_file() and not p.name.endswith((".json", ".part", ".md5mismatch"))
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


def downloads_are_current(lp: LayerPaths, manifest_path: Path, mod_count: int) -> bool:
    data = _read_json(lp.downloads_json)
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


def inspect_is_current(lp: LayerPaths) -> bool:
    downloads = _read_json(lp.downloads_json)
    data = _read_json(lp.inspect_json)
    if not downloads or not data:
        return False
    if not _same_path(data.get("downloads_json"), lp.downloads_json):
        return False
    expected = sum(
        1 for e in downloads.get("entries") or [] if e.get("status") in ("ok", "skipped")
    )
    return len(data.get("entries") or []) == expected and expected > 0


def install_is_current(lp: LayerPaths, manifest_path: Path) -> bool:
    inspected = _read_json(lp.inspect_json)
    data = _read_json(lp.install_json)
    if not inspected or not data:
        return False
    if not _same_path(data.get("manifest"), manifest_path):
        return False
    if not _same_path(data.get("mods_dir"), lp.mods):
        return False
    entries = data.get("entries") or []
    if len(entries) != len(inspected.get("entries") or []) or not entries:
        return False
    return all((lp.mods / (e.get("folder") or "")).is_dir() for e in entries)


def profile_is_current(paths: Paths, led: ledger.Ledger) -> bool:
    """True when the rendered profile describes exactly the layers the ledger has now."""
    data = _read_json(paths.profile_report)
    if not data or not _same_path(data.get("mo2_dir"), paths.out):
        return False
    rendered = [(r.get("slug"), str(r.get("revision"))) for r in data.get("layers") or []]
    wanted = [(r.get("slug"), str(r.get("revision"))) for r in led.data.get("layers") or []]
    if not wanted or rendered != wanted:
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


def _download_failures(downloads_json: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """`(unavailable, mismatched)` from a downloads.json: what could not be fetched.

    `unavailable` is `(mod name, error)` for archives Nexus would not serve at all --
    a pinned file the author has since deleted 404s forever. `mismatched` is archives
    that downloaded but whose md5 did not match the manifest, which is never something
    to shrug off: the file is not the one the collection was built against.
    """
    data = _read_json(downloads_json) or {}
    unavailable: list[tuple[str, str]] = []
    mismatched: list[str] = []
    for entry in data.get("entries") or []:
        status = entry.get("status")
        name = entry.get("name") or entry.get("file_name") or entry.get("tag") or "?"
        if status == "error":
            unavailable.append((name, entry.get("error") or "download failed"))
        elif status == "md5_mismatch":
            mismatched.append(name)
    return unavailable, mismatched


def _survey_is_current(survey_json: Path, manifest_path: Path) -> bool:
    data = _read_json(survey_json)
    if not data:
        return False
    return _same_path(data.get("manifest"), manifest_path) and bool(data.get("entries"))


# -- one layer ---------------------------------------------------------------------------


def _namespace(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@dataclass
class LayerContext:
    """What `add_layer` produced: enough to register the layer and report on it."""

    slug: str
    revision: int | str
    owner: str
    name: str
    author: str
    domain: str
    game_name: str
    manifest_path: Path
    layer_paths: LayerPaths
    installed: int = 0
    shared: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    is_base: bool = True


def add_layer(
    paths: Paths,
    args: argparse.Namespace,
    *,
    led: ledger.Ledger,
    api_key: str,
    game_path: Path,
    run: Run,
    rep: Reporter,
) -> LayerContext | None:
    """Fetch, download, inspect and install one collection into an existing instance.

    Shared by `create` (which calls it for the very first collection) and `add`. On
    return the ledger knows which `mods/` folders the layer owns and which it shares
    with a layer that was already there; it is not saved yet -- the caller does that
    once the profile has been rendered. Returns `None` when a stage failed, with the
    detail in `run`.
    """
    is_base = not (led.data.get("layers") or [])

    # -- fetch ---------------------------------------------------------------------
    rep.stage("fetch")
    try:
        ref = CollectionRef.parse(args.url)
        client = NexusClient(api_key=api_key)
        info = client.revision_info(ref, args.revision)
        rev_dir = paths.collections / ref.slug / str(info.revision_number)
        had_manifest = (
            any((rev_dir / "archive").rglob("collection.json")) if rev_dir.exists() else False
        )
        info, manifest_path = fetch_manifest(
            client, ref, args.revision, paths.collections, info=info
        )
    except (AuthRequired, NexusError, OSError, ValueError) as exc:
        run.record("fetch", "failed", str(exc))
        return None

    revision = info.revision_number
    owner = ledger.collection_owner(ref.slug, revision)
    lp = LayerPaths(paths, ref.slug, revision)
    manifest = load_manifest(manifest_path)
    manifest_info = manifest.get("info") or {}
    domain = manifest_info.get("domainName") or info.game
    try:
        game_name = mo2_game_name(domain)
    except NexusError as exc:
        run.record("fetch", "failed", str(exc))
        return None
    mod_count = len(manifest.get("mods") or [])

    if had_manifest:
        run.record("fetch", "skipped", f"{ref.slug} revision {revision} already fetched")
    else:
        run.record("fetch", "ok", f"{info.name} revision {revision}, {mod_count} mods")
        rep.done("fetch", f"{info.name} [{domain}] revision {revision} -> {manifest_path}")

    ctx = LayerContext(
        slug=ref.slug,
        revision=revision,
        owner=owner,
        name=manifest_info.get("name") or info.name,
        author=manifest_info.get("author") or "",
        domain=domain,
        game_name=game_name,
        manifest_path=manifest_path,
        layer_paths=lp,
        is_base=is_base,
    )

    # -- reuse an existing archive store -------------------------------------------
    reuse = getattr(args, "reuse_downloads", None)
    if reuse:
        src = Path(reuse).expanduser().resolve()
        if not src.is_dir():
            rep.warn(f"--reuse-downloads {src} is not a directory, ignored")
        elif src == paths.downloads:
            rep.warn("--reuse-downloads points at the instance's own downloads folder, ignored")
        else:
            linked, copied, present = reuse_downloads(src, paths.downloads, rep)
            run.record("reuse-downloads", "ok", f"{linked} linked, {copied} copied, {present} kept")

    # -- survey (optional pre-flight) ----------------------------------------------
    if getattr(args, "skip_survey", False):
        run.record("survey", "skipped", "--skip-survey")
    elif _survey_is_current(lp.survey_json, manifest_path):
        run.record("survey", "skipped", str(lp.survey_json))
    else:
        try:
            rc = survey.run_survey(
                manifest_path=manifest_path,
                out_path=lp.survey_json,
                jobs=args.jobs,
                survey_all=False,
                min_remaining=100,
                limit=None,
                api_key=api_key,
                reporter=rep,
            )
        except Exception as exc:  # noqa: BLE001 - the survey only informs the operator
            # Nothing downstream reads survey.json, so no failure in here -- a 404 on a
            # file the curator pinned from an archived mod page, a dropped connection --
            # is allowed to stop the run.
            rc, exc_text = 1, f"{type(exc).__name__}: {exc}"
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
    missing: list[str] = []
    if downloads_are_current(lp, manifest_path, mod_count):
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
                json_path=lp.downloads_json,
                reporter=rep,
            )
        except (AuthRequired, NexusError) as exc:
            run.record("download", "failed", str(exc))
            return None
        if rc != 0:
            unavailable, mismatched = _download_failures(lp.downloads_json)
            allow = getattr(args, "allow_missing", False)
            if not allow or mismatched or not unavailable:
                run.record("download", "failed", "one or more archives failed or mismatched")
                return None
            # A file the curator pinned that Nexus no longer serves (the author deleted
            # or archived it) can never be downloaded, so with --allow-missing the layer
            # goes in without it rather than the whole instance being unbuildable. The
            # mods it would have installed are simply absent, and named here.
            ctx_missing = unavailable
            rep.warn(
                f"{len(ctx_missing)} archive(s) are not available from Nexus; continuing "
                "without them (--allow-missing):"
            )
            for name, error in ctx_missing:
                rep.warn(f"  {name}: {error}")
            run.record("download", "warned", f"{len(ctx_missing)} archive(s) unavailable")
            missing = [name for name, _ in ctx_missing]
        else:
            run.record("download", "ok")

    # -- inspect --------------------------------------------------------------------
    if inspect_is_current(lp):
        run.record("inspect", "skipped", str(lp.inspect_json))
    else:
        rc = archive_inspect.cmd_inspect(
            _namespace(
                downloads_json=str(lp.downloads_json),
                out=str(lp.inspect_json),
                jobs=args.jobs,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("inspect", "failed", "one or more archives could not be listed")
            return None
        run.record("inspect", "ok")

    # -- install --------------------------------------------------------------------
    # Layer-aware folder naming: a mod whose name is already taken by another layer
    # keeps that folder when it is byte-for-byte the same archive (the two layers then
    # share one folder) and gets a ` ~<slug>` folder of its own when it is not.
    taken = {folder: (rec.get("md5") or "") for folder, rec in led.data["mods"].items()}
    if install_is_current(lp, manifest_path):
        run.record("install", "skipped", f"mods/ matches {lp.install_json.name}")
    else:
        rc = installer.cmd_install(
            _namespace(
                inspect_json=str(lp.inspect_json),
                manifest=str(manifest_path),
                mods_dir=str(paths.mods),
                only=None,
                game_path=str(game_path),
                jobs=args.jobs,
                force=False,
                choices_overrides=getattr(args, "choices_overrides", None),
                out=str(lp.install_json),
                owner=owner,
                taken_folders=taken,
                folder_suffix="" if is_base else ref.slug,
            ),
            reporter=rep,
        )
        if rc != 0:
            run.record("install", "failed", "one or more mods failed to install")
            return None
        run.record("install", "ok")

    # -- ownership -------------------------------------------------------------------
    ctx.missing = missing
    install_data = _read_json(lp.install_json) or {}
    for entry in install_data.get("entries") or []:
        folder = entry.get("folder")
        if not folder:
            continue
        record = led.data["mods"].get(folder)
        owners = led.owners_of(folder) if record else []
        if record and owner in owners and len(owners) > 1:
            # Already shared with another layer, and this is a re-run: leave the record
            # (and which layer is primary) exactly as it is.
            ctx.shared.append(folder)
            ctx.installed += 1
            continue
        if record and owner not in owners:
            if (record.get("md5") or "") == (entry.get("md5") or ""):
                # Same archive, same name: one folder, two owners, nothing reinstalled.
                led.add_mod_owner(folder, owner)
                ctx.shared.append(folder)
                ctx.installed += 1
                continue
            rep.warn(
                f"mods/{folder} already belongs to {', '.join(owners)} with a different "
                f"archive; {owner} is taking it over"
            )
        led.set_mod_owner(
            folder,
            owner,
            tag=entry.get("tag") or "",
            md5=entry.get("md5") or "",
            install_mode=entry.get("install_mode") or "",
            strategy=entry.get("strategy") or "",
            plugins=entry.get("plugins") or [],
        )
        ctx.installed += 1

    # A shared folder belongs to every layer that pinned it; say so in MO2's own view.
    for folder in ctx.shared:
        installer.stamp_owner(paths.mods / folder, ", ".join(led.owners_of(folder)))

    led.normalise_owner_order()
    led.register_layer(
        ref.slug,
        revision,
        name=ctx.name,
        author=ctx.author,
        separators=(led.layer(ref.slug, revision) or {}).get("separators") or [],
        manifest=manifest_path.resolve().relative_to(paths.out.resolve()).as_posix(),
        files=lp.ledger_files(),
    )
    return ctx


def render_profile(
    paths: Paths,
    led: ledger.Ledger,
    rep: Reporter,
    *,
    game_path: str | None = None,
    mo2_version: str = build.DEFAULT_MO2_VERSION,
    resolution: str = "keep",
    vsync: str = "keep",
    window: str = "keep",
    disable_optional: bool = False,
    keep_inis: bool = False,
    separators: bool = True,
) -> dict[str, Any]:
    """Render the instance profile from every layer in `led` (`profile.render_instance`)."""
    return profile.render_instance(
        paths.out,
        led=led,
        game_path=game_path,
        mo2_version=mo2_version,
        separators=separators,
        disable_optional=disable_optional,
        resolution=resolution,
        vsync=vsync,
        window=window,
        keep_inis=keep_inis,
        report_out=paths.profile_report,
        reporter=rep,
    )


# -- the command ------------------------------------------------------------------------


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

    # Before any of our own folders are created, or the legacy `c2wj/` rename below
    # would find `c2mo2/` already there and skip.
    migrate_legacy_instance(paths, rep)

    paths.stage.mkdir(parents=True, exist_ok=True)
    paths.downloads.mkdir(parents=True, exist_ok=True)
    paths.mods.mkdir(parents=True, exist_ok=True)

    led = ledger.load(paths.out)
    migrate_instance(paths, led, rep)
    layers = led.data.get("layers") or []
    if layers and layers[0].get("slug") != CollectionRef.parse(args.url).slug:
        rep.warn(
            f"{paths.out} was created from {layers[0].get('slug')}; `c2mo2 add` is the command "
            "for layering another collection on top of it"
        )

    ctx = add_layer(paths, args, led=led, api_key=api_key, game_path=game_path, run=run, rep=rep)
    if ctx is None:
        return _finish(run, rep, paths, started)

    led.set_game(
        domain=ctx.domain,
        mo2_name=ctx.game_name,
        source_path=str(game_path),
        stock_game_dir=(_read_json(paths.build_meta) or {}).get("stock_game_dir"),
    )
    led.set_mo2(version=args.mo2_version, rootbuilder_version=args.rootbuilder_version)
    led.save()

    # -- profile --------------------------------------------------------------------
    profile_skipped = profile_is_current(paths, led)
    if profile_skipped:
        run.record("profile", "skipped", str(paths.profile_report))
    else:
        try:
            render_profile(
                paths,
                led,
                rep,
                game_path=str(game_path),
                mo2_version=args.mo2_version,
                resolution=args.resolution,
                vsync=args.vsync,
                window=args.window,
            )
        except (OSError, ValueError) as exc:
            run.record("profile", "failed", str(exc))
            return _finish(run, rep, paths, started)
        run.record("profile", "ok")

    # -- build ----------------------------------------------------------------------
    # On a new instance `profile` writes ModOrganizer.ini with the *source* game folder
    # as gamePath, so build has to run to re-point it at the Stock Game copy.
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
    led.set_game(stock_game_dir=(_read_json(paths.build_meta) or {}).get("stock_game_dir"))
    led.save()

    mods_on_disk = led.scan_mods_dir(paths.mods)
    owned = len(led.mods_owned_by(ctx.owner))
    user_mods = [f for f, owners in mods_on_disk.items() if owners == ["user"]]
    detail = f"{owned} mods owned by {ctx.owner}"
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

    # -- tools (after the ledger save above: `tools install` writes the ledger itself) --
    tool_ids = list(dict.fromkeys(getattr(args, "tools", None) or []))
    if tool_ids:
        install_tools_stage(paths, tool_ids, run, rep)

    return _finish(run, rep, paths, started, mod_count=len(mods_on_disk))


def install_tools_stage(paths: Paths, tool_ids: list[str], run: Run, rep: Reporter) -> int:
    """`create --tools` / the wizard's Tools page: install catalogue tools into the new
    instance as the pipeline's last stage.

    Runs after the ledger stage has saved, because `tools.cmd_tools_install` loads and
    writes the ledger itself (recording each tool under `tools[id]`); running it earlier
    would let `create`'s in-memory ledger clobber those records. A failed tool is a
    failed stage in the summary, but the instance itself is complete and launchable --
    `c2mo2 tools install` or the Manage tab can retry.
    """
    from . import tools  # tools imports create (legacy migration); keep this one lazy

    rep.stage("tools", len(tool_ids))
    with stdout_to_reporter(rep):
        rc = tools.cmd_tools_install(
            _namespace(ids=tool_ids, mo2_dir=str(paths.out), all_default=False, force=False)
        )
    if rc != 0:
        run.record("tools", "failed", "one or more tools did not install; see the log above")
        return rc
    run.record("tools", "ok", ", ".join(tool_ids))
    rep.done("tools", f"installed {', '.join(tool_ids)}")
    return 0


def _finish(
    run: Run,
    rep: Reporter,
    paths: Paths,
    started: float,
    label: str = "create",
    mod_count: int | None = None,
) -> int:
    elapsed = time.monotonic() - started
    rep.stage("summary")
    for stage in run.stages:
        line = f"  {stage.status:<8} {stage.name}"
        if stage.detail:
            line += f"  ({stage.detail})"
        rep.log(line)
    rep.log(f"  elapsed: {elapsed:.1f}s")
    if run.failed:
        rep.warn(f"{label} did not finish: see the failed stage above")
        return 1
    rep.log("")
    rep.log(f"launch MO2:  {paths.out / 'ModOrganizer.exe'}")
    rep.log(f"ledger:      {paths.out / ledger.LEDGER_NAME}")
    if mod_count:
        rep.log(f"note: first start indexes {mod_count} mods and may take a minute")
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
        "--allow-missing",
        action="store_true",
        default=False,
        help="carry on when Nexus no longer serves a file the collection pinned (the "
        "author deleted it); those mods are left out and listed in the summary. An "
        "md5 mismatch still stops the run.",
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
    p.add_argument(
        "--tools",
        nargs="+",
        metavar="ID",
        default=[],
        help="catalogue tools to install once the instance is built (ids from `c2mo2 tools list`)",
    )
    p.set_defaults(func=cmd_create)
