"""A reusable, verbose progress display, wired to a `QtReporter`'s signals.

Top to bottom: a stage line ("Stage 3 of 7 - Downloading archives"), the overall bar
(per-stage progress), a counter ("112 / 292 files"), the current item (file or mod
name, elided in the middle if long), a rate line for downloads ("48.3 MB/s - 1.21 GB
of 1.88 GB - ETA 0:14") or elapsed time for other stages, then a log pane with
warnings tinted amber and errors red, auto-scrolling unless the user has scrolled up.

Both `pages/progress.py` (a full `create`/`add` pipeline run) and `pages/manage.py`
(a single Manage-tab action: add/remove/update a layer, install tools, export to
Wabbajack) embed one of these, so every `Reporter`-driven operation in the GUI gets
the same verbosity for free.
"""

from __future__ import annotations

import html
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QProgressBar, QTextEdit, QVBoxLayout, QWidget

from .progress_calc import RateEstimator, format_duration, format_rate_line
from .reporter_bridge import QtReporter
from .theme import MUTED_STYLE

# The `create`/`add` pipeline's stage order (see `create.py`'s module docstring and
# `add_layer`). Used only to derive a "Stage N of M" line when the engine call itself
# doesn't say so (`stage_index`/`stage_count` on `Reporter.stage()` are additive and
# nothing populates them yet -- see `reporter.py`). Stage names outside this list
# (Manage-tab actions like "tools", or bookkeeping stages like "ledger") just show
# their title with no "Stage N of M" prefix.
STAGE_SEQUENCE = ["fetch", "survey", "download", "inspect", "install", "profile", "build"]

STAGE_TITLES = {
    "fetch": "Fetching the collection",
    "survey": "Surveying FOMODs",
    "download": "Downloading archives",
    "inspect": "Inspecting archives",
    "install": "Installing mods",
    "profile": "Rendering the profile",
    "build": "Installing Mod Organizer",
    "reuse-downloads": "Reusing an existing download store",
    "ledger": "Finalizing",
    "summary": "Summary",
    "tools": "Installing tools",
}

# What a stage's `done`/`total` counts, for the counter line ("112 / 292 <unit>").
COUNTER_UNIT = {
    "fetch": "items",
    "survey": "mods",
    "download": "files",
    "inspect": "archives",
    "install": "mods",
    "reuse-downloads": "files",
    "tools": "tools",
}

_WARN_COLOR = "#d9a441"
_ERROR_COLOR = "#e05d5d"


def stage_position(name: str) -> tuple[int, int] | None:
    """`(index, count)`, 1-based, for a stage in the known pipeline order -- `None`
    for a stage this widget doesn't recognise (it just won't get a "Stage N of M")."""
    if name in STAGE_SEQUENCE:
        return STAGE_SEQUENCE.index(name) + 1, len(STAGE_SEQUENCE)
    return None


class ProgressWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stage_label = QLabel("")
        layout.addWidget(self.stage_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.counter_label = QLabel("")
        layout.addWidget(self.counter_label)

        self.current_item_label = QLabel("")
        self.current_item_label.setStyleSheet(MUTED_STYLE)
        layout.addWidget(self.current_item_label)

        self.rate_label = QLabel("")
        self.rate_label.setStyleSheet(MUTED_STYLE)
        layout.addWidget(self.rate_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        self._rate = RateEstimator()
        self._current_stage = ""
        self._stage_started_at = time.monotonic()
        self._byte_progress_active = False
        self._user_scrolled_up = False
        # False until `reset()` marks a new operation as started; gates both the
        # elapsed-time tick (`_on_tick`) and (indirectly, since `_on_stage` only ever
        # fires while an operation is running) the indeterminate bar -- see the class
        # docstring in the module header and `reset()`/`finish()` below.
        self._operation_running = False
        self.log_view.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Ticks the elapsed-time line for stages with no bytes-level progress (fetch,
        # profile, build, ...), which otherwise only ever get a done()/stage() call.
        # Runs continuously; `_on_tick` itself no-ops while idle or once `finish()`
        # has been called, so nothing needs to start/stop this timer.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

    # -- wiring -----------------------------------------------------------------------

    def attach(self, reporter: QtReporter) -> None:
        """Connect this widget to every signal of `reporter`. Call `reset()` first if
        the widget is being reused for a new run/action."""
        reporter.stageStarted.connect(self._on_stage)
        reporter.progressed.connect(self._on_progress)
        reporter.logged.connect(self.log_message)
        reporter.warned.connect(lambda m: self.log_message(m, level="warn"))
        reporter.stageDone.connect(self._on_done)

    def reset(self, operation_name: str | None = None) -> None:
        """Call right before starting a new operation (a fresh `EngineWorker`/
        `reporter`). Marks the widget as busy: the bar goes indeterminate ("Starting
        ...", total not known yet) and the elapsed-time tick resumes. Pair with
        `finish()` once the operation completes/fails/is cancelled.

        `operation_name`, when given, means "this widget is being reused for another
        operation on the same page" (Manage-tab actions): instead of clearing the log
        pane, a timestamped separator line is appended so earlier operations' output
        stays readable. Omit it (the default) to clear the log outright, as a brand
        new `create`/`add` pipeline run on the Progress page does."""
        if operation_name is not None:
            self.log_message(f"----- {operation_name} - {time.strftime('%H:%M:%S')} -----")
        else:
            self.log_view.clear()
        self.stage_label.setText("Starting...")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.counter_label.setText("")
        self.current_item_label.setText("")
        self.rate_label.setText("")
        self._rate.reset()
        self._current_stage = ""
        self._stage_started_at = time.monotonic()
        self._byte_progress_active = False
        self._user_scrolled_up = False
        self._operation_running = True

    def finish(self) -> None:
        """Call once the operation this widget is tracking completes, fails, or is
        cancelled. Freezes the elapsed-time line at its last value (the tick stops
        updating it) and, if the bar was left indeterminate (a stage with no known
        total was still "current" when the operation ended), settles it back to a
        determinate 0% rather than leaving it spinning forever."""
        self._operation_running = False
        self._byte_progress_active = False
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)

    # -- log pane: tinted warnings/errors, auto-scroll unless the user scrolled up ----

    def _on_scroll(self, value: int) -> None:
        bar = self.log_view.verticalScrollBar()
        # A couple of pixels of slop: "at the bottom" should still count as "at the
        # bottom" even after the scrollbar's range shifts from a just-appended line.
        self._user_scrolled_up = value < bar.maximum() - 2

    def log_message(self, text: str, level: str = "info") -> None:
        """Append one log line. `level` is `"info"` (default), `"warn"` (amber,
        prefixed `warning:`) or `"error"` (red)."""
        color = {"warn": _WARN_COLOR, "error": _ERROR_COLOR}.get(level)
        line = f"warning: {text}" if level == "warn" else text
        if color:
            # `append()` only parses HTML when the string looks like markup -- a
            # plain line with no tags is inserted verbatim (entities and all), so
            # escaping only happens on this branch, where a real `<span>` is added.
            self.log_view.append(f'<span style="color:{color};">{html.escape(line)}</span>')
        else:
            self.log_view.append(line)
        if not self._user_scrolled_up:
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    # -- current-item text, elided in the middle if it doesn't fit -------------------

    def _elide(self, text: str) -> str:
        fm = QFontMetrics(self.current_item_label.font())
        width = self.current_item_label.width() or self.width() or 400
        return fm.elidedText(text, Qt.TextElideMode.ElideMiddle, max(80, width))

    # -- reporter slots ----------------------------------------------------------------

    def _on_stage(self, name: str, total, stage_index, stage_count) -> None:
        self._current_stage = name
        self._stage_started_at = time.monotonic()
        self._rate.reset()
        self._byte_progress_active = False
        if stage_index is None or stage_count is None:
            position = stage_position(name)
            if position is not None:
                stage_index, stage_count = position
        title = STAGE_TITLES.get(name, name)
        if stage_index and stage_count:
            self.stage_label.setText(f"Stage {stage_index} of {stage_count} - {title}")
        else:
            self.stage_label.setText(title)
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(0)
        else:
            self.progress_bar.setRange(0, 0)
        self.counter_label.setText("")
        self.current_item_label.setText("")
        self.rate_label.setText("")
        self.log_message(f"== {title}")

    def _on_progress(self, done: int, total, label: str, bytes_done, bytes_total) -> None:
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            unit = COUNTER_UNIT.get(self._current_stage, "items")
            self.counter_label.setText(f"{done} / {total} {unit}")
        if label:
            self.current_item_label.setText(self._elide(label))
        if bytes_done is not None:
            self._byte_progress_active = True
            self._rate.update(bytes_done, time.monotonic())
            rate = self._rate.rate_bytes_per_sec()
            eta = self._rate.eta_seconds(bytes_total)
            self.rate_label.setText(format_rate_line(bytes_done, bytes_total, rate, eta))
        else:
            self._byte_progress_active = False
            self._update_elapsed()

    def _on_done(self, name: str, summary: str) -> None:
        self.log_message(f"-- {name}: {summary}" if summary else f"-- {name}")

    def _on_tick(self) -> None:
        if self._operation_running and self._current_stage and not self._byte_progress_active:
            self._update_elapsed()

    def _update_elapsed(self) -> None:
        elapsed = time.monotonic() - self._stage_started_at
        self.rate_label.setText(f"Elapsed {format_duration(elapsed)}")
