"""Recently-opened Manage instances, persisted with `QSettings`.

Stored under organisation "collections2wabbajack", app "c2wj-gui" -- the Windows
registry under `HKEY_CURRENT_USER\\Software\\collections2wabbajack\\c2wj-gui` in
production; tests redirect `QSettings` at a temp INI file instead (see
`tests/test_gui_smoke.py`'s `_isolated_qsettings` fixture).

**Deliberately not** `QSettings(ORGANIZATION, APPLICATION)` -- that two-argument
convenience constructor hard-codes `QSettings::NativeFormat` and ignores
`QSettings.setDefaultFormat()`/`setPath()` entirely (verified against this PySide6
build), so a test redirecting those statics to a temp dir would silently still hit the
real registry. `_settings()` below passes `QSettings.defaultFormat()` explicitly
through the four-argument constructor, which *does* honour `setPath()`.

Only `Path.is_file()` (via `api.instance_exists`) is used to decide whether a recent
entry is still valid -- no network call, so pruning is safe to do on every read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QSettings

from .. import api

ORGANIZATION = "collections2wabbajack"
APPLICATION = "c2wj-gui"

_ARRAY_KEY = "recent_instances"
MAX_RECENTS = 8


@dataclass(frozen=True)
class RecentInstance:
    path: str
    collection_name: str
    last_opened: str  # ISO 8601, UTC


def _settings() -> QSettings:
    return QSettings(
        QSettings.defaultFormat(), QSettings.Scope.UserScope, ORGANIZATION, APPLICATION
    )


def load_recents(*, prune: bool = True) -> list[RecentInstance]:
    """Recent instances, most-recently-opened first.

    When `prune` (the default), entries whose `c2wj-instance.json` no longer exists
    are dropped and the pruned list is written back immediately.
    """
    settings = _settings()
    size = settings.beginReadArray(_ARRAY_KEY)
    items: list[RecentInstance] = []
    for i in range(size):
        settings.setArrayIndex(i)
        path = str(settings.value("path", ""))
        if not path:
            continue
        items.append(
            RecentInstance(
                path=path,
                collection_name=str(settings.value("collection_name", "")),
                last_opened=str(settings.value("last_opened", "")),
            )
        )
    settings.endArray()

    if not prune:
        return items
    valid = [r for r in items if api.instance_exists(r.path)]
    if len(valid) != len(items):
        save_recents(valid)
    return valid


def save_recents(recents: list[RecentInstance]) -> None:
    settings = _settings()
    settings.beginWriteArray(_ARRAY_KEY)
    for i, r in enumerate(recents):
        settings.setArrayIndex(i)
        settings.setValue("path", r.path)
        settings.setValue("collection_name", r.collection_name)
        settings.setValue("last_opened", r.last_opened)
    settings.endArray()
    settings.sync()


def remember_instance(path: str | Path, collection_name: str) -> None:
    """Move `path` to the front of the recents list (adding it if new). Called after a
    successful Manage "Load" and after a successful `create`."""
    path_str = str(Path(path))
    recents = [r for r in load_recents(prune=False) if r.path != path_str]
    recents.insert(
        0,
        RecentInstance(
            path=path_str,
            collection_name=collection_name,
            last_opened=datetime.now(UTC).isoformat(),
        ),
    )
    save_recents(recents[:MAX_RECENTS])


def most_recent_valid() -> RecentInstance | None:
    recents = load_recents(prune=True)
    return recents[0] if recents else None
