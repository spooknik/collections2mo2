"""Page 8: runs `api.create_instance` on a worker thread and shows its progress."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from ... import api
from ..reporter_bridge import QtReporter
from ..worker import EngineWorker
from .base import WizardPage


class ProgressPage(WizardPage):
    title = "Running"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._worker: EngineWorker | None = None

        layout = QVBoxLayout(self)
        self.stage_label = QLabel("")
        layout.addWidget(self.stage_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate until a stage reports a total
        layout.addWidget(self.progress_bar)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        btn_row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_btn)
        self.launch_btn = QPushButton("Launch Mod Organizer")
        self.launch_btn.clicked.connect(self._launch)
        self.launch_btn.setVisible(False)
        btn_row.addWidget(self.launch_btn)
        self.open_folder_btn = QPushButton("Open folder")
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.open_folder_btn.setVisible(False)
        btn_row.addWidget(self.open_folder_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

    def start(self) -> None:
        """Called by the window right after switching to this page."""
        s = self.state
        self.log_view.clear()
        self.stage_label.setText("Starting...")
        self.progress_bar.setRange(0, 0)
        self.cancel_btn.setEnabled(True)
        self.launch_btn.setVisible(False)
        self.open_folder_btn.setVisible(False)

        reporter = QtReporter()
        reporter.stageStarted.connect(self._on_stage)
        reporter.progressed.connect(self._on_progress)
        reporter.logged.connect(self._append)
        reporter.warned.connect(lambda m: self._append(f"warning: {m}"))
        reporter.stageDone.connect(self._on_done)

        kwargs = {
            "url": s.collection_url,
            "out": s.instance_dir,
            "game_path": s.game_path,
            "revision": s.selected_revision,
            "stock_game": s.stock_game,
            "jobs": s.jobs,
            "resolution": s.resolution,
            "vsync": s.vsync,
            "window": s.window,
            "skip_survey": True,
            "allow_missing": False,
        }
        self._worker = EngineWorker(api.create_instance, kwargs, reporter=reporter)
        self._worker.succeeded.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.reporter.cancel()
            self.cancel_btn.setEnabled(False)
            self.stage_label.setText("Cancelling (stops between stages)...")

    def _append(self, text: str) -> None:
        self.log_view.append(text)

    def _on_stage(self, name: str, total) -> None:
        self.stage_label.setText(name)
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(0)
        else:
            self.progress_bar.setRange(0, 0)
        self._append(f"== {name}")

    def _on_progress(self, done: int, total, label: str) -> None:
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _on_done(self, name: str, summary: str) -> None:
        self._append(f"-- {name}: {summary}" if summary else f"-- {name}")

    def _on_finished(self, rc: int) -> None:
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        if rc == 0:
            self.progress_bar.setValue(1)
            self.stage_label.setText("Done.")
            self.state.run_succeeded = True
            self.launch_btn.setVisible(True)
            self.open_folder_btn.setVisible(True)
        else:
            self.progress_bar.setValue(0)
            self.stage_label.setText("Did not finish -- see the log above.")
            self.state.run_succeeded = False

    def _on_failed(self, message: str) -> None:
        self.cancel_btn.setEnabled(False)
        self.stage_label.setText("Failed.")
        self._append(f"error: {message}")
        self.state.run_succeeded = False

    def _on_cancelled(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.stage_label.setText("Cancelled.")
        self._append("cancelled by user")
        self.state.run_succeeded = False

    def _launch(self) -> None:
        if self.state.instance_dir is not None:
            api.launch_mod_organizer(self.state.instance_dir)

    def _open_folder(self) -> None:
        if self.state.instance_dir is not None:
            api.open_folder(self.state.instance_dir)

    def on_leave(self) -> bool:
        return False
