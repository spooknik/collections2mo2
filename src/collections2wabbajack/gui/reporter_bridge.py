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

`progress()` calls can arrive very fast during a busy stage -- `downloader.py` now
reports one `progress()` call per completed file *and* one about every 2 MiB of a large
file's chunks, so a 292-mod download can easily fire well past a thousand calls.
Emitting a Qt signal (and thus a UI repaint) for every one of them would stall the
event loop, so `progress()` only stashes the latest call's arguments; an internal
`QTimer` flushes at most once every `throttle_ms` (~10 Hz by default), so downstream
slots see a bounded update rate no matter how fast the worker thread calls in.
`stage()` and `done()` flush immediately (and, for `stage()`, reset the throttle
window) so a stage boundary is never delayed behind a stale progress reading.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from ..api import OperationCancelled


class QtReporter(QObject):
    stageStarted = Signal(str, object, object, object)  # name, total, stage_index, stage_count
    progressed = Signal(
        int, object, str, object, object
    )  # done, total, label, bytes_done, bytes_total
    logged = Signal(str)
    warned = Signal(str)
    stageDone = Signal(str, str)  # name, summary

    def __init__(self, parent: QObject | None = None, throttle_ms: int = 100):
        super().__init__(parent)
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._pending: tuple[int, int | None, str, int | None, int | None] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, throttle_ms))
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    # -- cancellation, called from the GUI thread ---------------------------------

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def reset(self) -> None:
        self._cancel_event.clear()

    # -- Reporter protocol, called from the worker thread --------------------------

    def stage(
        self,
        name: str,
        total: int | None = None,
        *,
        stage_index: int | None = None,
        stage_count: int | None = None,
    ) -> None:
        if self._cancel_event.is_set():
            raise OperationCancelled(f"cancelled before stage {name!r}")
        self._flush()
        self.stageStarted.emit(name, total, stage_index, stage_count)

    def progress(
        self,
        done: int,
        total: int | None,
        label: str = "",
        *,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        if self._cancel_event.is_set():
            raise OperationCancelled("cancelled")
        with self._lock:
            self._pending = (done, total, label, bytes_done, bytes_total)

    def log(self, msg: str) -> None:
        self.logged.emit(msg)

    def warn(self, msg: str) -> None:
        self.warned.emit(msg)

    def done(self, name: str, summary: str = "") -> None:
        self._flush()
        self.stageDone.emit(name, summary)

    # -- throttling ------------------------------------------------------------------

    def _flush(self) -> None:
        """Emit the most recently stashed `progress()` call, if any arrived since the
        last flush. Safe to call from any thread; harmless when nothing is pending."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            self.progressed.emit(*pending)
