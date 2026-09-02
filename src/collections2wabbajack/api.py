"""Thin facade the GUI calls into the engine through.

Nothing under `gui/` imports engine modules (`create`, `layers`, `tools`, `nexus`, ...)
directly -- every engine call the GUI makes goes through a function here, with explicit
keyword arguments and (where the call can take a while) a `reporter=` parameter. If an
engine function's signature drifts, the fix is in this one file.

Two other things live here besides the wrapper functions:

- **Sign-in helpers** (`validate_api_key`, `*_api_key` keyring helpers, `activate_api_key`)
  because the engine has no notion of "the signed-in user" -- it only reads
  `NEXUS_API_KEY` from the environment/`.env` (see `cli.py: _client()`). The GUI stores
  the key with `keyring` and calls `activate_api_key` before any engine call.
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
to a persistent per-user folder when running frozen, or when `C2WJ_DATA_DIR` is set
explicitly. This does not edit `sevenzip.py` / `build.py` / `tools.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.errors

from . import build, create, layers, ledger, profile, survey, tools
from . import sevenzip as sevenzip_mod
from . import tools as tools_mod
from .manifest import fetch_manifest, load_manifest
from .nexus import API_BASE, AuthRequired, CollectionRef, NexusClient, NexusError
from .reporter import NullReporter, Reporter, get_reporter

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
    "activate_api_key",
    "add_collection_layer",
    "clear_api_key",
    "create_instance",
    "default_instance_dir",
    "detect_skyrim_se_path",
    "dir_size_bytes",
    "disk_free_bytes",
    "export_to_wabbajack",
    "fetch_collection_summary",
    "format_bytes",
    "get_saved_api_key",
    "has_update_support",
    "has_wabbajack_support",
    "install_more_tools",
    "install_tools",
    "launch_mod_organizer",
    "list_revisions",
    "list_tool_groups",
    "load_instance",
    "nexus_api_key_signup_url",
    "open_folder",
    "path_warnings",
    "remove_collection_layer",
    "run_fomod_survey",
    "save_api_key",
    "update_collection_layer",
    "validate_api_key",
]


class ApiError(RuntimeError):
    """Something the GUI should show the user, not a bug in the GUI itself."""


class OperationCancelled(Exception):
    """Raised by a GUI reporter bridge to unwind a running engine call between stages."""


# -- packaging: keep 7-Zip / MO2 caches out of a wiped PyInstaller temp dir -----------


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "collections2wabbajack"


def _apply_data_dir_override() -> Path | None:
    override = os.environ.get("C2WJ_DATA_DIR")
    if not override and getattr(sys, "frozen", False):
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

KEYRING_SERVICE = "collections2wabbajack"
KEYRING_USERNAME = "nexus-api-key"
NEXUS_API_KEY_URL = "https://www.nexusmods.com/users/myaccount?tab=api+access"


def nexus_api_key_signup_url() -> str:
    return NEXUS_API_KEY_URL


def get_saved_api_key() -> str | None:
    """The API key stored in the OS credential store, or `None` if there isn't one."""
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.KeyringError:
        return None


def save_api_key(api_key: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)


def clear_api_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


def activate_api_key(api_key: str) -> None:
    """Make `api_key` the one every engine call in this process sees.

    The engine reads `NEXUS_API_KEY` from the environment (`cli.py: _client()` etc.,
    via `python-dotenv`'s `load_dotenv()`, which by default does not override an
    already-set environment variable) -- so setting it here before any engine call
    is enough, and safe even if the repo's own `.env` also has a key in it.
    """
    os.environ["NEXUS_API_KEY"] = api_key


@dataclass(frozen=True)
class SignInResult:
    name: str
    is_premium: bool


def validate_api_key(api_key: str) -> SignInResult:
    """`GET /v1/users/validate.json` -- confirms the key and whether it is Premium.

    Raises `ApiError` with a message fit to show the user on any failure.
    """
    if not api_key or not api_key.strip():
        raise ApiError("Paste your Nexus personal API key first.")
    client = NexusClient(api_key=api_key.strip())
    try:
        resp = client.session.get(f"{API_BASE}/v1/users/validate.json", timeout=30)
    except Exception as exc:
        raise ApiError(f"Could not reach Nexus Mods: {exc}") from exc
    if resp.status_code == 401:
        raise ApiError("Nexus rejected that key. Copy it again from your account page.")
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
  }
}
"""


def fetch_collection_summary(
    url: str, *, revision: int | None = None, api_key: str | None = None
) -> CollectionSummary:
    """Metadata for a collection URL without downloading anything (`nexus.py`'s
    anonymous GraphQL path -- a key is accepted but not required)."""
    try:
        ref = CollectionRef.parse(url)
    except NexusError as exc:
        raise ApiError(str(exc)) from exc
    client = NexusClient(api_key=api_key)
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
    api_key: str,
    jobs: int = 4,
    reporter: Reporter | None = None,
) -> SurveySummary:
    """Fetch the manifest (if not already cached) and run `survey.run_survey` on it.

    Non-blocking in intent only insofar as the caller runs this off the UI thread; it
    still makes network calls and can take a while on a large collection, which is why
    it is rate-limit-aware (mirrors `c2wj survey`'s exit code 3) and safe to re-run --
    results are cached in `GUI_CACHE_DIR` keyed by slug/revision.
    """
    rep = get_reporter(reporter)
    try:
        ref = CollectionRef.parse(url)
    except NexusError as exc:
        raise ApiError(str(exc)) from exc
    client = NexusClient(api_key=api_key)
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
            api_key=api_key,
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
            "ok", "survey complete", len(targets), fetched, len(fresh_fomod),
            [e.name for e in fresh_fomod],
        )
    if rc == 3:
        return SurveySummary(
            "rate_limited",
            "Nexus's hourly API budget ran out; re-run later to finish the survey.",
            len(targets), fetched, len(fresh_fomod), [e.name for e in fresh_fomod],
        )
    return SurveySummary(
        "error", "the survey did not complete", len(targets), fetched,
        len(fresh_fomod), [e.name for e in fresh_fomod],
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


def path_warnings(path: str | Path) -> list[str]:
    """Human-readable warnings about `path` as an instance location; empty if it's fine."""
    text = str(Path(path))
    warnings: list[str] = []
    if len(text) > create.PATH_WARN_LEN:
        warnings.append(
            f"This path is {len(text)} characters long. Windows caps full paths at 260, and "
            f"mod files nest deeply inside the instance -- {create.PATH_WARN_LEN} or fewer "
            "(e.g. D:\\Skyrim) leaves room for the deepest file."
        )
    lowered = text.lower()
    if "onedrive" in lowered:
        warnings.append(
            "This path is inside OneDrive. OneDrive can sync or lock files mid-install; pick "
            "a folder outside it."
        )
    try:
        documents = str(Path.home() / "Documents").lower()
    except OSError:
        documents = ""
    if documents and lowered.startswith(documents):
        warnings.append(
            "This path is under Documents. A dedicated folder (e.g. D:\\Skyrim) is usually "
            "faster and avoids backup/antivirus tools scanning tens of thousands of mod files."
        )
    return warnings


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
    return [Path(m.group(1).replace("\\\\", "\\")) for m in re.finditer(r'"path"\s*"([^"]+)"', text)]


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


class _LineReporterStream(io.TextIOBase):
    """Redirects `print()`-based output (tools.py has no `Reporter` hooks) to a Reporter."""

    def __init__(self, rep: Reporter):
        self._rep = rep
        self._buf = ""

    def write(self, text: str) -> int:  # type: ignore[override]
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._rep.log(line)
        return len(text)

    def flush(self) -> None:
        return None


@contextlib.contextmanager
def _stdout_to_reporter(rep: Reporter):
    with contextlib.redirect_stdout(_LineReporterStream(rep)):
        yield


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
    reuse_downloads: str | None = None,
    jobs: int = 4,
    resolution: str = "keep",
    vsync: str = "keep",
    window: str = "keep",
    choices_overrides: str | None = None,
    skip_survey: bool = True,
    allow_missing: bool = False,
    mo2_version: str = build.DEFAULT_MO2_VERSION,
    rootbuilder_version: str = build.DEFAULT_ROOTBUILDER_VERSION,
    reporter: Reporter | None = None,
) -> int:
    """`c2wj create`: collection URL -> a runnable, self-contained MO2 instance.

    `resolution` is validated the same way the CLI does (`'auto'`, `'keep'`, or `WxH`).
    """
    resolution = profile._parse_resolution_arg(resolution)
    ns = argparse.Namespace(
        url=url,
        out=str(out),
        game_path=str(game_path),
        revision=revision,
        stock_game=stock_game,
        reuse_downloads=reuse_downloads,
        jobs=jobs,
        resolution=resolution,
        vsync=vsync,
        window=window,
        choices_overrides=choices_overrides,
        skip_survey=skip_survey,
        allow_missing=allow_missing,
        mo2_version=mo2_version,
        rootbuilder_version=rootbuilder_version,
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


@dataclass(frozen=True)
class InstanceSummary:
    out: Path
    game_domain: str
    game_name: str
    mo2_version: str
    layers: list[LayerStatus]
    user_mod_count: int


def load_instance(instance_dir: str | Path) -> InstanceSummary:
    """Read an existing instance's ledger and (network permitting) each layer's latest
    revision on Nexus, for the Manage tab. Does not require an API key -- collection
    metadata is anonymous GraphQL -- but a key gets more reliable results."""
    paths = create.Paths(Path(instance_dir).expanduser().resolve())
    if not (paths.out / ledger.LEDGER_NAME).exists():
        raise ApiError(f"{paths.out} is not a c2wj instance ({ledger.LEDGER_NAME} not found).")

    led = ledger.load(paths.out)
    game = led.data.get("game") or {}
    api_key = os.environ.get("NEXUS_API_KEY") or None
    client = NexusClient(api_key=api_key)

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
    )


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
    reuse_downloads: str | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`c2wj add`: layer another collection on top of an existing instance."""
    ns = argparse.Namespace(
        url=url,
        instance=str(instance_dir),
        revision=revision,
        jobs=jobs,
        game_path=str(game_path) if game_path else None,
        choices_overrides=choices_overrides,
        skip_survey=skip_survey,
        allow_missing=allow_missing,
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
    """`c2wj remove`: remove a collection layer, keeping shared and user mods."""
    ns = argparse.Namespace(
        slug=slug,
        instance=str(instance_dir),
        purge_downloads=purge_downloads,
        force=force,
    )
    return layers.cmd_remove(ns, reporter=reporter)


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
    purge_old: bool = False,
    choices_overrides: str | None = None,
    reporter: Reporter | None = None,
) -> int:
    """`c2wj update`: move a layer to a newer revision, applying only the delta.

    `--yes` is always passed -- `update.cmd_update` otherwise prompts on a terminal
    the GUI does not have (`_confirm` in `update.py`), which would hang the worker
    thread forever.
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
    """`c2wj wabbajack`: compile an instance into a `.wabbajack` modlist."""
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
