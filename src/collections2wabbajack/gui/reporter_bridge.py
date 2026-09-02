"""A `collections2wabbajack.reporter.Reporter` that talks to the Qt GUI thread.

`QtReporter` is a `QObject` with one signal per `Reporter` method. It is constructed on
the GUI thread (so its signals queue safely to GUI-thread slots) and handed to a worker
thread, which calls its plain methods -- Qt signal emission is safe from any thread; the
connected slots run on the receiver's thread. It also carries the cancel flag: `cancel()`
(called from the GUI thread) sets a `threading.Event`; `stage()` and `progress()` (called
from the worker thread, between/within pipeline stages) raise `api.OperationCancelled`
when it is set, which unwinds the engine call cleanly -- `create.cmd_create` and friends
only catch specific exception types around individual stages, so this exception propagates
straight out of e.g. `create.cmd_create` to the worker's `run()`.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ..api import OperationCancelled


class QtReporter(QObject):
    stageStarted = Signal(str, object)  # name, total|None
    progressed = Signal(int, object, str)  # done, total|None, label
    logged = Signal(str)
    warned = Signal(str)
    stageDone = Signal(str, str)  # name, summary

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cancel_event = threading.Event()

    # -- cancellation, called from the GUI thread ---------------------------------

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def reset(self) -> None:
        self._cancel_event.clear()

    # -- Reporter protocol, called from the worker thread --------------------------

    def stage(self, name: str, total: int | None = None) -> None:
        if self._cancel_event.is_set():
            raise OperationCancelled(f"cancelled before stage {name!r}")
        self.stageStarted.emit(name, total)

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        if self._cancel_event.is_set():
            raise OperationCancelled("cancelled")
        self.progressed.emit(done, total, label)

    def log(self, msg: str) -> None:
        self.logged.emit(msg)

    def warn(self, msg: str) -> None:
        self.warned.emit(msg)

    def done(self, name: str, summary: str = "") -> None:
        self.stageDone.emit(name, summary)
