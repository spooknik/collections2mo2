"""Thin facade the GUI calls into the engine through.

Nothing under `gui/` imports engine modules (`create`, `layers`, `tools`, `nexus`, ...)
directly -- every engine call the GUI makes goes through a function here, with explicit
keyword arguments and (where the call can take a while) a `reporter=` parameter. If an
engine function's signature drifts, the fix is in this one file.

Two other things live here besides the wrapper functions:

- **Sign-in helpers** (`sign_in`, `check_signin`, `sign_out`, `current_auth`) over
  `oauth.py`: the browser-based OAuth sign-in Nexus requires of a public app. The engine
  itself picks the sign-in up through `oauth.default_auth()` (the credential store, or a
  developer's `NEXUS_API_KEY`), so the GUI only has to make sure one exists.
- **GUI-only lookups** (Steam game detection, default instance path, disk usage, path
  warnings) that have no engine equivalent because the CLI always takes `--game-path`
  and `--out` as explicit arguments.

Packaging note: `sevenzip.TOOLS_DIR` and `build.CACHE_DIR` (and `tools.CACHE_DIR`) are
computed from `Path(__file__).resolve().parents[2]` at import time, i.e. two directories
above the *installed* module -- fine for `uv run`, but a PyInstaller onefile build runs
from a temporary extraction directory that is wiped after every run, so 7-Zip and MO2
would re-bootstrap (multi-hundred-MB downloads) on every launch. `_apply_data_dir_override`
below reassigns those three module attributes (a plain global lookup at call time in all
three modules, verified by reading them -- so reassignment after import takes effect)
to a persistent per-user folder when running frozen, or when `C2MO2_DATA_DIR` is set
explicitly. This does not edit `sevenzip.py` / `build.py` / `tools.py`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.errors

from . import build, create, game_version, layers, ledger, oauth, profile, survey, tools
from . import sevenzip as sevenzip_mod
from . import tools as tools_mod
from .manifest import fetch_manifest, load_manifest
from .nexus import API_BASE, AuthRequired, CollectionRef, NexusAuth, NexusClient, NexusError
from .reporter import NullReporter, Reporter, get_reporter, stdout_to_reporter

__all__ = [
    "ApiError",
    "CollectionSummary",
    "InstanceSummary",
    "LayerStatus",
    "NullReporter",
    "OperationCancelled",
    "RevisionChoice",
    "SignInResult",
    "SurveySummary",
    "ToolEntry",
    "add_collection_layer",
    "check_signin",
    "create_instance",
    "current_auth",
    "default_instance_dir",
    "detect_skyrim_se_path",
    "dir_size_bytes",
    "disk_free_bytes",
    "downloads_path_warnings",
    "export_to_wabbajack",
    "fetch_collection_summary",
    "forget_legacy_api_key",
    "format_bytes",
    "game_version_check",
    "has_saved_signin",
    "has_update_support",
    "has_wabbajack_support",
    "install_more_tools",
    "install_tools",
    "installed_game_version",
    "instance_downloads_dir",
    "instance_exists",
    "launch_mod_organizer",
    "list_revisions",
    "list_tool_groups",
    "load_instance",
    "nexus_authorized_apps_url",
    "open_folder",
    "path_warnings",
    "remove_collection_layer",
    "run_fomod_survey",
    "short_game_version",
    "sign_in",
    "sign_out",
    "skipped_mods",
    "update_collection_layer",
]


class ApiError(RuntimeError):
    """Something the GUI should show the user, not a bug in the GUI itself."""


class OperationCancelled(Exception):
    """Raised by a GUI reporter bridge to unwind a running engine call between stages."""


# -- packaging: keep 7-Zip / MO2 caches out of a wiped PyInstaller temp dir -----------


LEGACY_DATA_DIR_NAME = "collections2wabbajack"
LEGACY_DATA_DIR_ENV = "C2WJ_DATA_DIR"


def _default_data_dir() -> Path:
    """The per-user cache folder for 7-Zip / MO2 downloads.

    The project used to be called `collections2wabbajack`; an install that already
    bootstrapped its tools under the old folder keeps using it, so a rename does not
    cost the user a multi-hundred-MB re-download. New installs get the new name.
    """
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    current = base / "collections2mo2"
    if not current.exists():
        legacy = base / LEGACY_DATA_DIR_NAME
        if legacy.is_dir():
            return legacy
    return current


def _is_packaged() -> bool:
    """True inside a packaged build: PyInstaller sets `sys.frozen`; Nuitka does not, and
    instead defines `__compiled__` in every module it compiled (verified with a probe
    build of Nuitka 4.2, 2026-09-03: `sys.frozen` is absent there)."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _apply_data_dir_override() -> Path | None:
    override = os.environ.get("C2MO2_DATA_DIR") or os.environ.get(LEGACY_DATA_DIR_ENV)
    if not override and _is_packaged():
        override = str(_default_data_dir())
    if not override:
        return None
    base = Path(override)
    sevenzip_mod.TOOLS_DIR = base / "tools"
    build.CACHE_DIR = base / "tools" / "cache"
    tools_mod.CACHE_DIR = base / "tools" / "cache"
    return base


DATA_DIR_OVERRIDE = _apply_data_dir_override()

# Where the GUI stashes a fetched collection.json before an instance folder is chosen
# (the "Check FOMODs" pre-flight on the Collection page runs before Location/game).
GUI_CACHE_DIR = (DATA_DIR_OVERRIDE or _default_data_dir()) / "gui-cache"


# -- sign-in ----------------------------------------------------------------------------

# Where the pre-0.2.0 GUI kept the user's personal API key. Public apps may not use
# personal keys (Nexus API Acceptable Use Policy), so the entry is only ever deleted now.
KEYRING_SERVICE = "collections2mo2"
LEGACY_KEYRING_SERVICE = "collections2wabbajack"
LEGACY_KEYRING_USERNAME = "nexus-api-key"


def nexus_authorized_apps_url() -> str:
    """The Nexus page where the user can revoke this app's access to their account."""
    return oauth.AUTHORIZED_APPS_URL


def forget_legacy_api_key() -> None:
    """Delete the personal API key an older release stored, if any."""
    for service in (KEYRING_SERVICE, LEGACY_KEYRING_SERVICE):
        try:
            if keyring.get_password(service, LEGACY_KEYRING_USERNAME) is None:
                continue
            keyring.delete_password(service, LEGACY_KEYRING_USERNAME)
        except keyring.errors.KeyringError:
            pass


@dataclass(frozen=True)
class SignInResult:
    name: str
    is_premium: bool


def has_saved_signin() -> bool:
    """Whether a sign-in exists to try (an OAuth token pair in the credential store, or
    a developer's `NEXUS_API_KEY`). It may still turn out to be revoked: `check_signin`."""
    return oauth.default_auth() is not None


def current_auth() -> NexusAuth | None:
    """The credentials the engine will use, for GUI calls that take an `auth=`."""
    return oauth.default_auth()


def sign_in(*, cancel: threading.Event | None = None) -> SignInResult:
    """Run the browser sign-in (`oauth.login`) and confirm the account with Nexus.

    Opens the system browser on Nexus's authorise page and waits for the redirect; set
    `cancel` to abandon the wait (raises `OperationCancelled`). Raises `ApiError` with a
    message fit to show the user on any failure.
    """
    try:
        oauth.login(cancel=cancel)
    except oauth.LoginCancelled as exc:
        raise OperationCancelled(str(exc)) from exc
    except (oauth.OAuthError, NexusError) as exc:
        raise ApiError(str(exc)) from exc
    forget_legacy_api_key()
    return check_signin()


def sign_out() -> None:
    oauth.sign_out()
    forget_legacy_api_key()


def check_signin() -> SignInResult:
    """`GET /v1/users/validate.json` with the current sign-in: confirms Nexus accepts it
    and whether the account is Premium.

    A rejected OAuth sign-in (revoked on Nexus, or an aged-out refresh token) is cleared
    from the credential store here, so the GUI's next start goes straight to Sign in.
    Raises `ApiError` with a message fit to show the user on any failure.
    """
    auth = oauth.default_auth()
    if auth is None:
        raise ApiError("Not signed in to Nexus Mods.")
    client = NexusClient(auth)
    try:
        resp = client.session.get(f"{API_BASE}/v1/users/validate.json", timeout=30)
    except oauth.SignedOut as exc:
        sign_out()
        raise ApiError(str(exc)) from exc
    except Exception as exc:
        raise ApiError(f"Could not reach Nexus Mods: {exc}") from exc
    if resp.status_code == 401:
        if isinstance(auth, oauth.BearerAuth):
            sign_out()
            raise ApiError("Nexus Mods no longer accepts this sign-in; sign in again.")
        raise ApiError("Nexus rejected the API key in NEXUS_API_KEY.")
    try:
        resp.raise_for_status()
    except Exception as exc:
        raise ApiError(f"Nexus returned an error: {exc}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise ApiError("Nexus returned an unexpected response.") from exc
    name = body.get("name") or "?"
    is_premium = bool(body.get("is_premium"))
    if isinstance(auth, oauth.BearerAuth):
        # The token's own claims are the fallback if validate.json ever stops carrying
        # these fields for OAuth callers.
        tokens = auth.tokens
        if name == "?" and tokens.username:
            name = tokens.username
        if "is_premium" not in body and tokens.is_premium is not None:
            is_premium = tokens.is_premium
    return SignInResult(name=name, is_premium=is_premium)


# -- collection metadata (anonymous GraphQL) ---------------------------------------------


@dataclass(frozen=True)
class RevisionChoice:
    revision_number: int
    status: str


@dataclass(frozen=True)
class CollectionSummary:
    url: str
    slug: str
    game_domain: str
    name: str
    summary: str
    author: str
    mod_count: int
    total_size: int
    revision_number: int
    latest_revision_number: int
    revisions: list[RevisionChoice] = field(default_factory=list)
    # The game versions the revision was built against ("1.6.1170.0"), same list the
    # manifest carries as `info.gameVersions`. Empty when the field is missing.
    game_versions: list[str] = field(default_factory=list)


_SUMMARY_QUERY = """
query($slug: String!, $revision: Int) {
  collection(slug: $slug, viewAdultContent: true) {
    name summary
    game { domainName }
    user { name }
    latestPublishedRevision { revisionNumber }
    revisions { revisionNumber status }
  }
  collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true) {
    revisionNumber modCount totalSize downloadLink
    gameVersions { reference }
  }
}
"""


def _game_versions(revision: dict[str, Any] | None) -> list[str]:
    """`gameVersions { reference }` from a `collectionRevision` payload, as strings."""
    entries = (revision or {}).get("gameVersions") or []
    out: list[str] = []
    for entry in entries:
        reference = entry.get("reference") if isinstance(entry, dict) else entry
        if reference and str(reference).strip():
            out.append(str(reference).strip())
    return out


def fetch_collection_summary(
    url: str, *, revision: int | None = None, auth: NexusAuth | None = None
) -> CollectionSummary:
    """Metadata for a collection URL without downloading anything (`nexus.py`'s
    anonymous GraphQL path -- a sign-in is accepted but not required)."""
    try:
        ref = CollectionRef.parse(url)
    except NexusError as exc:
        raise ApiError(str(exc)) from exc
    client = NexusClient(auth)
    try:
        data = client.graphql(_SUMMARY_QUERY, {"slug": ref.slug, "revision": revision})
    except (NexusError, AuthRequired) as exc:
        raise ApiError(str(exc)) from exc
    coll = data.get("collection")
    rev = data.get("collectionRevision")
    if not coll:
        raise ApiError(f"Collection '{ref.slug}' was not found.")
    if not rev:
        raise ApiError(f"Revision {revision} of '{ref.slug}' was not found.")
    revisions = [
        RevisionChoice(int(r["revisionNumber"]), r.get("status") or "")
        for r in (coll.get("revisions") or [])
        if r.get("revisionNumber") is not None
    ]
    revisions.sort(key=lambda r: r.revision_number, reverse=True)
    latest = (coll.get("latestPublishedRevision") or {}).get("revisionNumber")
    return CollectionSummary(
        url=url,
        slug=ref.slug,
        game_domain=(coll.get("game") or {}).get("domainName") or ref.game,
        name=coll.get("name") or ref.slug,
        summary=coll.get("summary") or "",
        author=(coll.get("user") or {}).get("name") or "",
        mod_count=int(rev.get("modCount") or 0),
        total_size=int(rev.get("totalSize") or 0),
        revision_number=int(rev["revisionNumber"]),
        latest_revision_number=int(latest) if latest is not None else int(rev["revisionNumber"]),
        revisions=revisions,
        game_versions=_game_versions(rev),
    )


def list_revisions(summary: CollectionSummary) -> list[RevisionChoice]:
    return [r for r in summary.revisions if r.status == "published"] or summary.revisions


# -- survey (optional pre-flight) ---------------------------------------------------------


@dataclass(frozen=True)
class SurveySummary:
    status: str  # "ok" | "rate_limited" | "error"
    detail: str
    targets: int
    fetched: int
    fresh_fomod_count: int
    fresh_fomod_names: list[str]


def run_fomod_survey(
    url: str,
    *,
    revision: int | None,
    auth: NexusAuth | None = None,
    jobs: int = 4,
    reporter: Reporter | None = None,
) -> SurveySummary:
    """Fetch the manifest (if not already cached) and run `survey.run_survey` on it.

    Non-blocking in intent only insofar as the caller runs this off the UI thread; it
    still makes network calls and can take a while on a large collection, which is why
    it is rate-limit-aware (mirrors `c2mo2 survey`'s exit code 3) and safe to re-run --
    results are cached in `GUI_CACHE_DIR` keyed by slug/revision.
    """
    rep = get_reporter(reporter)
    try:
        ref = CollectionRef.parse(url)
    except NexusError as exc:
        raise ApiError(str(exc)) from exc
    if auth is None:
        auth = oauth.default_auth()
    client = NexusClient(auth)
    try:
        info, manifest_path = fetch_manifest(client, ref, revision, GUI_CACHE_DIR / "collections")
    except (AuthRequired, NexusError, OSError, ValueError) as exc:
        raise ApiError(str(exc)) from exc

    out_path = GUI_CACHE_DIR / "survey" / ref.slug / f"{info.revision_number}.survey.json"
    try:
        rc = survey.run_survey(
            manifest_path=manifest_path,
            out_path=out_path,
            jobs=jobs,
            survey_all=False,
            min_remaining=100,
            limit=None,
            auth=auth,
            reporter=rep,
        )
    except AuthRequired as exc:
        raise ApiError(str(exc)) from exc
    except NexusError as exc:
        return SurveySummary("error", str(exc), 0, 0, 0, [])

    state = survey.SurveyState.load(out_path)
    entries = list(state.entries.values()) if state else []
    fresh_fomod = [e for e in entries if e.install_mode == "fresh" and e.has_fomod]
    manifest = load_manifest(manifest_path)
    targets = survey._select_targets(manifest.get("mods") or [], False)
    fetched = sum(1 for e in entries if e.preview_fetched)

    if rc == 0:
        return SurveySummary(
            "ok",
            "survey complete",
            len(targets),
            fetched,
            len(fresh_fomod),
            [e.name for e in fresh_fomod],
        )
    if rc == 3:
        return SurveySummary(
            "rate_limited",
            "Nexus's hourly API budget ran out; re-run later to finish the survey.",
            len(targets),
            fetched,
            len(fresh_fomod),
            [e.name for e in fresh_fomod],
        )
    return SurveySummary(
        "error",
        "the survey did not complete",
        len(targets),
        fetched,
        len(fresh_fomod),
        [e.name for e in fresh_fomod],
    )


# -- install location / game detection -----------------------------------------------------


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name).strip().rstrip(".")
    return cleaned or "Instance"


def default_instance_dir(collection_name: str) -> Path:
    safe = _sanitize_name(collection_name)
    if Path("D:/").exists():
        return Path("D:/") / safe
    return Path("C:/Modding") / safe


def path_warnings(path: str | Path, game_path: str | Path | None = None) -> list[str]:
    """Human-readable warnings about `path` as an instance location; empty if it's fine.

    The implementation lives in `create.instance_path_warnings` (this module imports
    `create`, not the other way round) so `c2mo2 create` prints exactly what the wizard
    shows. `game_path`, when the caller knows it, adds the "inside the game folder" case.
    """
    return create.instance_path_warnings(path, game_path)


def downloads_path_warnings(
    downloads: str | Path,
    instance: str | Path | None = None,
    game_path: str | Path | None = None,
) -> list[str]:
    """Human-readable warnings about `downloads` as a custom archive store; empty if fine.

    Delegates to `create.downloads_path_warnings` for the same reason `path_warnings`
    delegates: `c2mo2 create --downloads-dir` prints exactly what the wizard shows.
    `instance`, when known, adds the "inside the instance's mods/overwrite" cases.
    """
    return create.downloads_path_warnings(downloads, instance, game_path)


def instance_downloads_dir(instance_dir: str | Path) -> Path | None:
    """The custom archive store an existing instance records, or None for the default.

    An offline, ledger-only read (unlike `load_instance`, which also asks Nexus for each
    layer's latest revision), so the wizard can call it on the UI thread when it prefills
    the Location page for a folder the user is reusing.
    """
    base = Path(instance_dir)
    downloads = ledger.downloads_dir(base)
    default = base / ledger.DEFAULT_DOWNLOADS_NAME
    return None if downloads == default else downloads


def game_version_check(
    collection_versions: list[str],
    game_path: str | Path | None,
    game_name: str | None = "skyrimspecialedition",
) -> tuple[str, str] | None:
    """`(status, message)` comparing a collection's target game version with the installed
    one, or None when the collection lists no version. Status: "match"/"mismatch"/"unknown".

    A local file read (the exe's version resource), so the GUI can call it on the UI
    thread. Never blocks anything: a mismatch is a warning the user can act on later,
    because a `Stock Game` copy can be downgraded or patched after the build.
    """
    return game_version.check_game_version(collection_versions, game_path, game_name)


def installed_game_version(
    game_path: str | Path | None, game_name: str | None = "skyrimspecialedition"
) -> str | None:
    """The game's own version as Windows reports it, or None if it can't be read."""
    return game_version.installed_game_version(game_path, game_name)


def short_game_version(version: str) -> str:
    """`1.6.1170.0` -> `1.6.1170` (the form collections and curators quote)."""
    return game_version.short_version(version)


def _steam_install_path() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg

    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    )
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "InstallPath")
        except OSError:
            continue
        if value:
            return Path(value)
    return None


def _steam_library_paths(steam_root: Path) -> list[Path]:
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return []
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return [
        Path(m.group(1).replace("\\\\", "\\")) for m in re.finditer(r'"path"\s*"([^"]+)"', text)
    ]


def detect_skyrim_se_path() -> Path | None:
    """Best-effort Steam autodetection of the Skyrim Special Edition install folder."""
    steam_root = _steam_install_path()
    if steam_root is None:
        return None
    libraries = [steam_root, *_steam_library_paths(steam_root)]
    for lib in libraries:
        candidate = lib / "steamapps" / "common" / "Skyrim Special Edition"
        if candidate.is_dir():
            return candidate
    return None


def dir_size_bytes(path: str | Path) -> int:
    """Total size of everything under `path`. Can be slow on a large game folder --
    call it off the UI thread."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _existing_ancestor(path: str | Path) -> Path:
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def disk_free_bytes(path: str | Path) -> int:
    return shutil.disk_usage(_existing_ancestor(path)).free


def format_bytes(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


# -- tools catalogue --------------------------------------------------------------------


@dataclass(frozen=True)
class ToolEntry:
    id: str
    name: str
    group: str
    default: bool
    requires: str
    note: str
    size_hint_mb: int | None
    disabled: bool
    status: str  # "installed" | "not installed" | "unavailable"


def list_tool_groups(mo2_dir: str | Path | None = None) -> list[tuple[str, list[ToolEntry]]]:
    """The tools catalogue (`tools_catalog.json`), grouped in catalogue order.

    When `mo2_dir` is a real instance, entries are marked installed/not against its
    ledger (same file `tools.py` itself writes to, read here via `ledger.load` rather
    than `tools.py`'s private loader).
    """
    catalog = tools.load_catalog()
    installed_tools: dict[str, Any] = {}
    mo2_path = Path(mo2_dir).resolve() if mo2_dir else None
    if mo2_path is not None and mo2_path.is_dir():
        create.migrate_legacy_instance(create.Paths(mo2_path), NullReporter())
    if mo2_path is not None and (mo2_path / ledger.LEDGER_NAME).exists():
        installed_tools = ledger.load(mo2_path).data.get("tools") or {}

    groups: dict[str, list[ToolEntry]] = {}
    for entry in catalog:
        install_cfg = entry.get("install") or {}
        disabled = bool(install_cfg.get("disabled"))
        if disabled:
            status = "unavailable"
        elif (
            mo2_path is not None
            and entry["id"] in installed_tools
            and (mo2_path / "Tools" / entry["id"]).is_dir()
        ):
            status = "installed"
        else:
            status = "not installed"
        groups.setdefault(entry["group"], []).append(
            ToolEntry(
                id=entry["id"],
                name=entry["name"],
                group=entry["group"],
                default=bool(entry.get("default")),
                requires="; ".join(entry.get("requires") or []),
                note=install_cfg.get("note") or "",
                size_hint_mb=entry.get("size_hint_mb"),
                disabled=disabled,
                status=status,
            )
        )
    return list(groups.items())


_stdout_to_reporter = stdout_to_reporter


def install_tools(
    mo2_dir: str | Path,
    tool_ids: list[str],
    *,
    all_default: bool = False,
    force: bool = False,
    reporter: Reporter | None = None,
) -> bool:
    """Install `tool_ids` (+ every `default=true` catalogue entry if `all_default`)."""
    rep = get_reporter(reporter)
    ns = argparse.Namespace(
        ids=list(tool_ids), mo2_dir=str(mo2_dir), all_default=all_default, force=force
    )
    rep.stage("tools", len(tool_ids) or None)
    with _stdout_to_reporter(rep):
        rc = tools.cmd_tools_install(ns)
    rep.done("tools", "installed" if rc == 0 else "one or more tools failed")
    return rc == 0


# -- create ------------------------------------------------------------------------------


def create_instance(
    *,
    url: str,
    out: str | Path,
    game_path: str | Path,
    revision: int | None = None,
    stock_game: bool = False,
    downloads_dir: str | Path | None = None,
    reuse_downloads: str | None = None,
    jobs: int = 4,
    resolution: str = "keep",
    vsync: str = "keep",
    window: str = "keep",
    choices_overrides: str | None = None,
    skip_survey: bool = True,
    allow_missing: bool = False,
    skip_errors: bool = False,
    mo2_version: str = build.DEFAULT_MO2_VERSION,
    rootbuilder_version: str = build.DEFAULT_ROOTBUILDER_VERSION,
    tool_ids: list[str] | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`c2mo2 create`: collection URL -> a runnable, self-contained MO2 instance.

    `resolution` is validated the same way the CLI does (`'auto'`, `'keep'`, or `WxH`).
    `tool_ids` are catalogue tools to install into the new instance once it is built
    (the wizard's Tools page; `--tools` on the CLI).
    `downloads_dir` puts the archive store somewhere other than `<out>/downloads`
    (`--downloads-dir`); None keeps the default, which is what the ledger records.
    `skip_errors` (`--skip-errors`) carries the run past any mod that cannot be
    downloaded, listed or installed instead of failing; the ones it dropped are on the
    layer's ledger record afterwards, which is what `skipped_mods` reads back.
    """
    resolution = profile._parse_resolution_arg(resolution)
    ns = argparse.Namespace(
        url=url,
        out=str(out),
        game_path=str(game_path),
        revision=revision,
        stock_game=stock_game,
        downloads_dir=str(downloads_dir) if downloads_dir else None,
        reuse_downloads=reuse_downloads,
        jobs=jobs,
        resolution=resolution,
        vsync=vsync,
        window=window,
        choices_overrides=choices_overrides,
        skip_survey=skip_survey,
        allow_missing=allow_missing,
        skip_errors=skip_errors,
        mo2_version=mo2_version,
        rootbuilder_version=rootbuilder_version,
        tools=list(tool_ids or []),
    )
    return create.cmd_create(ns, reporter=reporter)


# -- manage: instance status ---------------------------------------------------------------


@dataclass(frozen=True)
class LayerStatus:
    slug: str
    revision: int
    name: str
    author: str
    is_base: bool
    mod_count: int
    latest_revision_number: int | None
    update_available: bool
    # Mods the last run on this layer went in without (`--skip-errors`), each a
    # `{name, stage, reason}` dict straight off the ledger; empty for a clean run.
    skipped: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class InstanceSummary:
    out: Path
    game_domain: str
    game_name: str
    mo2_version: str
    layers: list[LayerStatus]
    user_mod_count: int
    # The archive store this instance actually uses: `<out>/downloads` unless the ledger
    # records a custom one (`create --downloads-dir`).
    downloads_dir: Path


def instance_exists(instance_dir: str | Path) -> bool:
    """Cheap, offline existence check for the GUI's recent-instances list -- true if
    `instance_dir` still holds a c2mo2 ledger. Unlike `load_instance`, this touches
    neither the network nor every layer's revision, so it is safe to call for every
    entry on startup (pruning stale recents)."""
    try:
        base = Path(instance_dir)
        return (base / ledger.LEDGER_NAME).is_file() or (base / ledger.LEGACY_LEDGER_NAME).is_file()
    except OSError:
        return False


def load_instance(instance_dir: str | Path) -> InstanceSummary:
    """Read an existing instance's ledger and (network permitting) each layer's latest
    revision on Nexus, for the Manage tab. Does not require an API key -- collection
    metadata is anonymous GraphQL -- but a key gets more reliable results."""
    paths = create.Paths.for_instance(instance_dir)
    create.migrate_legacy_instance(paths, NullReporter())
    if not (paths.out / ledger.LEDGER_NAME).exists():
        raise ApiError(f"{paths.out} is not a c2mo2 instance ({ledger.LEDGER_NAME} not found).")

    led = ledger.load(paths.out)
    game = led.data.get("game") or {}
    client = NexusClient(oauth.default_auth())

    layer_statuses: list[LayerStatus] = []
    for i, layer in enumerate(led.data.get("layers") or []):
        slug = layer.get("slug") or ""
        revision = int(layer.get("revision") or 0)
        owner = led.layer_owner(layer)
        mod_count = len(led.mods_owned_by(owner))
        latest: int | None = None
        try:
            ref = CollectionRef(game=game.get("domain") or "unknown", slug=slug)
            info = client.revision_info(ref, None)
            latest = info.revision_number
        except Exception:  # noqa: BLE001 - purely informational, never fatal to the Manage tab
            latest = None
        layer_statuses.append(
            LayerStatus(
                slug=slug,
                revision=revision,
                name=layer.get("name") or slug,
                author=layer.get("author") or "",
                is_base=(i == 0),
                mod_count=mod_count,
                latest_revision_number=latest,
                update_available=bool(latest and latest > revision),
                skipped=led.layer_skipped(layer),
            )
        )

    user_mods = [f for f, owners in led.scan_mods_dir(paths.mods).items() if owners == ["user"]]
    return InstanceSummary(
        out=paths.out,
        game_domain=game.get("domain") or "",
        game_name=game.get("mo2_name") or "",
        mo2_version=(led.data.get("mo2") or {}).get("version") or "",
        layers=layer_statuses,
        user_mod_count=len(user_mods),
        downloads_dir=led.downloads_dir,
    )


def skipped_mods(instance_dir: str | Path) -> list[dict[str, str]]:
    """Every mod an instance's layers were built without (`--skip-errors`), flattened.

    Each item is the ledger's `{name, stage, reason}` plus a `"layer"` key naming the
    layer's slug. Offline and ledger-only -- unlike `load_instance` it asks Nexus
    nothing -- so the wizard can call it on the UI thread the moment a run finishes.
    Empty when the folder holds no ledger at all.
    """
    try:
        led = ledger.load(instance_dir)
    except (OSError, ValueError):
        return []
    out: list[dict[str, str]] = []
    for layer in led.data.get("layers") or []:
        slug = layer.get("slug") or ""
        for item in led.layer_skipped(layer):
            out.append({**item, "layer": slug})
    return out


def add_collection_layer(
    *,
    instance_dir: str | Path,
    url: str,
    revision: int | None = None,
    game_path: str | Path | None = None,
    jobs: int = 4,
    choices_overrides: str | None = None,
    skip_survey: bool = True,
    allow_missing: bool = False,
    skip_errors: bool = False,
    reuse_downloads: str | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`c2mo2 add`: layer another collection on top of an existing instance.

    `skip_errors` (`--skip-errors`) is the same "carry on past a mod that will not
    download or install" switch `create_instance` takes; see `skipped_mods`.
    """
    ns = argparse.Namespace(
        url=url,
        instance=str(instance_dir),
        revision=revision,
        jobs=jobs,
        game_path=str(game_path) if game_path else None,
        choices_overrides=choices_overrides,
        skip_survey=skip_survey,
        allow_missing=allow_missing,
        skip_errors=skip_errors,
        reuse_downloads=reuse_downloads,
    )
    return layers.cmd_add(ns, reporter=reporter)


def remove_collection_layer(
    *,
    instance_dir: str | Path,
    slug: str,
    purge_downloads: bool = False,
    force: bool = False,
    reporter: Reporter | None = None,
) -> int:
    """`c2mo2 remove`: remove a collection layer, keeping shared and user mods."""
    ns = argparse.Namespace(
        slug=slug,
        instance=str(instance_dir),
        purge_downloads=purge_downloads,
        force=force,
    )
    return layers.cmd_remove(ns, reporter=reporter)


def _clear_readonly(func, path: str, _exc) -> None:
    """`shutil.rmtree` hook: clear the read-only bit and retry.

    Same pattern as `installer._clear_readonly` / `layers._clear_readonly` -- MO2 and
    some mod archives ship read-only files that Windows refuses to unlink. Unlike the
    installer's version this one does *not* swallow the retry's failure: a genuinely
    locked file (MO2 still running on the instance) has to reach `delete_instance`,
    which turns it into a message the user can act on.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def delete_instance(instance_dir: str | Path, *, reporter: Reporter | None = None) -> None:
    """Delete a whole c2mo2 instance folder -- MO2, the Stock Game copy, `mods/`,
    `downloads/`, installed tools, the lot. There is no undo.

    Only the instance folder goes: an archive store the ledger points somewhere else
    (`create --downloads-dir`) is outside that tree and is left untouched, so the
    downloads survive to seed the next instance.

    Refuses anything that is not recognisably one of our instances (no ledger) and any
    drive root, so a mistyped path can never take out an unrelated folder. The GUI's
    "Remove instance..." button is the only caller; it also drops the folder from the
    recent-instances list afterwards (`gui.recents.forget_instance`) -- `api.py` never
    imports the GUI.
    """
    rep = get_reporter(reporter)
    target = Path(instance_dir).expanduser().resolve()

    if target.parent == target or str(target) == target.anchor:
        raise ApiError(f"Refusing to delete {target}: that is a drive root, not an instance.")
    if not (
        (target / ledger.LEDGER_NAME).is_file() or (target / ledger.LEGACY_LEDGER_NAME).is_file()
    ):
        raise ApiError(
            f"{target} is not a c2mo2 instance ({ledger.LEDGER_NAME} not found), "
            "so it will not be deleted."
        )

    rep.stage("delete")
    rep.log(f"deleting {target}")
    try:
        shutil.rmtree(target, onexc=_clear_readonly)
    except OSError as exc:  # PermissionError (locked file) is an OSError subclass
        locked = getattr(exc, "filename", None) or str(target)
        raise ApiError(
            f"{target} could not be fully removed: {locked} is in use or could not be "
            "deleted. Close Mod Organizer 2 (and anything else using this folder) and "
            "try again. Whatever was already deleted has been deleted -- the folder is "
            "now a partial instance."
        ) from exc
    rep.done("delete", f"removed {target}")


def install_more_tools(
    instance_dir: str | Path,
    tool_ids: list[str],
    *,
    force: bool = False,
    reporter: Reporter | None = None,
) -> bool:
    """Alias of `install_tools` for the Manage tab's "Install more tools"."""
    return install_tools(instance_dir, tool_ids, force=force, reporter=reporter)


# -- optional engine modules developed concurrently (update.py, wabbajack.py) -----------
#
# Both landed while this GUI was being built. `_try_import` still guards every use --
# harmless if a future refactor ever removes one again -- but the wrappers below now
# call their real, verified entry points (`update.cmd_update` / `wabbajack.cmd_wabbajack`)
# instead of guessing at a function name.


def _try_import(name: str):
    import importlib

    try:
        return importlib.import_module(f".{name}", __package__)
    except ImportError:
        return None


def has_update_support() -> bool:
    """Whether `update.py` has landed yet. `load_instance` above already shows each
    layer's latest Nexus revision on its own (plain GraphQL); this only gates the
    actual "update this layer" action, which needs `update.py`'s diff/apply logic."""
    return _try_import("update") is not None


def has_wabbajack_support() -> bool:
    return _try_import("wabbajack") is not None


def update_collection_layer(
    *,
    instance_dir: str | Path,
    slug: str | None = None,
    to: str = "latest",
    dry_run: bool = False,
    jobs: int = 4,
    allow_missing: bool = False,
    skip_errors: bool = False,
    purge_old: bool = False,
    choices_overrides: str | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`c2mo2 update`: move a layer to a newer revision, applying only the delta.

    `--yes` is always passed -- `update.cmd_update` otherwise prompts on a terminal
    the GUI does not have (`_confirm` in `update.py`), which would hang the worker
    thread forever. `skip_errors` (`--skip-errors`) lets the delta apply past a mod
    the new revision cannot fetch or install; see `skipped_mods`.
    """
    module = _try_import("update")
    if module is None:
        raise ApiError("Updating a layer needs update.py, which is not in this build yet.")
    ns = argparse.Namespace(
        instance=str(instance_dir),
        layer=slug,
        to=to,
        dry_run=dry_run,
        yes=True,
        jobs=jobs,
        allow_missing=allow_missing,
        skip_errors=skip_errors,
        purge_old=purge_old,
        choices_overrides=choices_overrides,
    )
    return module.cmd_update(ns, reporter=reporter)


def export_to_wabbajack(
    instance_dir: str | Path,
    *,
    name: str | None = None,
    version: str | None = None,
    author: str | None = None,
    description: str | None = None,
    website: str | None = None,
    readme: str | None = None,
    image: str | None = None,
    output: str | None = None,
    wabbajack_cli: str | None = None,
    dry_run: bool = False,
    reporter: Reporter | None = None,
) -> int:
    """`c2mo2 wabbajack`: compile an instance into a `.wabbajack` modlist."""
    module = _try_import("wabbajack")
    if module is None:
        raise ApiError("Wabbajack export is not available yet in this build.")
    ns = argparse.Namespace(
        instance=str(instance_dir),
        name=name,
        version=version,
        author=author,
        description=description,
        website=website,
        readme=readme,
        image=image,
        output=output,
        wabbajack_cli=wabbajack_cli,
        dry_run=dry_run,
    )
    return module.cmd_wabbajack(ns, reporter=reporter)


# -- post-run -----------------------------------------------------------------------------


def launch_mod_organizer(instance_dir: str | Path) -> None:
    exe = Path(instance_dir) / "ModOrganizer.exe"
    if not exe.exists():
        raise ApiError(f"{exe} does not exist.")
    os.startfile(exe)


def open_folder(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise ApiError(f"{p} does not exist.")
    os.startfile(p)
