"""A generic worker thread so engine calls (`api.py` functions) never block the UI.

Every long-running `api.py` call is invoked as `EngineWorker(fn, kwargs, reporter)`:
`fn` is called on a background `QThread` with `kwargs`, plus `reporter=` if (and only
if) `fn` actually declares that parameter -- most `api.py` calls do (they wrap a
multi-stage pipeline), but a few one-shot lookups (`validate_api_key`,
`fetch_collection_summary`, `dir_size_bytes`, ...) don't. The result or exception comes
back on the GUI thread via `succeeded` / `failed` signals.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..api import ApiError, OperationCancelled
from .reporter_bridge import QtReporter


class EngineWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        fn: Callable[..., Any],
        kwargs: dict[str, Any],
        reporter: QtReporter | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._fn = fn
        self._kwargs = dict(kwargs)
        self._wants_reporter = "reporter" in inspect.signature(fn).parameters
        self.reporter = reporter or QtReporter()
        if reporter is None:
            # Keep it alive for the worker's lifetime when the caller didn't supply one.
            self.reporter.setParent(self)

    def run(self) -> None:
        try:
            if self._wants_reporter:
                result = self._fn(**self._kwargs, reporter=self.reporter)
            else:
                result = self._fn(**self._kwargs)
        except OperationCancelled:
            self.cancelled.emit()
            return
        except ApiError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(result)
