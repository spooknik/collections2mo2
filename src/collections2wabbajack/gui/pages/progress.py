"""Page 8: runs `api.create_instance` on a worker thread and shows its progress."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout

from ... import api, ledger
from .. import recents
from ..progress_widget import ProgressWidget
from ..reporter_bridge import QtReporter
from ..worker import EngineWorker
from .base import WizardPage

# MO2's first start on a freshly built instance indexes every mod folder before its UI
# is usable, which for a large collection can take well over a minute; without this the
# window just looks hung. Matches `create.py`'s CLI summary hint.
MO2_FIRST_START_SECONDS = 60


class ProgressPage(WizardPage):
    title = "Running"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._worker: EngineWorker | None = None
        self._launch_timer: QTimer | None = None

        layout = QVBoxLayout(self)
        self.panel = ProgressWidget()
        layout.addWidget(self.panel, 1)

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
        self.back_btn = QPushButton("Back to start")
        self.back_btn.clicked.connect(lambda: self.custom_action.emit("reset:home"))
        self.back_btn.setVisible(False)
        btn_row.addWidget(self.back_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

    def start(self) -> None:
        """Called by the window right after switching to this page."""
        s = self.state
        self.panel.reset()
        self.cancel_btn.setEnabled(True)
        self.launch_btn.setVisible(False)
        self.open_folder_btn.setVisible(False)
        self.back_btn.setVisible(False)
        self.busy_changed.emit(True)

        reporter = QtReporter()
        self.panel.attach(reporter)

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
            self.panel.stage_label.setText("Cancelling (stops between stages)...")

    def request_cancel(self) -> None:
        """Called by the window when the user chooses to quit while this page's run
        is still in progress (see `WizardWindow.closeEvent`)."""
        self._cancel()

    def _on_finished(self, rc: int) -> None:
        self.cancel_btn.setEnabled(False)
        self.panel.finish()
        self.panel.progress_bar.setRange(0, 1)
        if rc == 0:
            self.panel.progress_bar.setValue(1)
            self.panel.stage_label.setText("Done.")
            self.state.run_succeeded = True
            self.launch_btn.setVisible(True)
            self.open_folder_btn.setVisible(True)
            self._remember_instance()
        else:
            self.panel.progress_bar.setValue(0)
            self.panel.stage_label.setText("Did not finish -- see the log above.")
            self.state.run_succeeded = False
        self.back_btn.setVisible(True)
        self.busy_changed.emit(False)

    def _remember_instance(self) -> None:
        if self.state.instance_dir is None:
            return
        name = (
            self.state.collection_summary.name
            if self.state.collection_summary
            else self.state.instance_dir.name
        )
        recents.remember_instance(self.state.instance_dir, name)

    def _on_failed(self, message: str) -> None:
        self.cancel_btn.setEnabled(False)
        self.panel.finish()
        self.panel.stage_label.setText("Failed.")
        self.panel.log_message(f"error: {message}", level="error")
        self.state.run_succeeded = False
        self.back_btn.setVisible(True)
        self.busy_changed.emit(False)

    def _on_cancelled(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.panel.finish()
        self.panel.stage_label.setText("Cancelled.")
        self.panel.log_message("cancelled by user")
        self.state.run_succeeded = False
        self.back_btn.setVisible(True)
        self.busy_changed.emit(False)

    def _launch(self) -> None:
        if self.state.instance_dir is None:
            return
        api.launch_mod_organizer(self.state.instance_dir)

        mod_count = self._mod_count()
        self.launch_btn.setEnabled(False)
        self.panel.progress_bar.setRange(0, 0)  # indeterminate
        self.panel.stage_label.setText(
            f"Mod Organizer is starting and indexing {mod_count} mods "
            "(first start can take a minute or more)"
        )
        self._launch_timer = QTimer(self)
        self._launch_timer.setSingleShot(True)
        self._launch_timer.timeout.connect(self._on_launch_settled)
        self._launch_timer.start(MO2_FIRST_START_SECONDS * 1000)

    def _mod_count(self) -> int:
        if self.state.instance_dir is None:
            return 0
        try:
            return len(ledger.load(self.state.instance_dir).data.get("mods") or {})
        except Exception:  # noqa: BLE001 - this is a cosmetic hint, never worth failing over
            return 0

    def _on_launch_settled(self) -> None:
        self.launch_btn.setEnabled(True)
        self.panel.progress_bar.setRange(0, 1)
        self.panel.progress_bar.setValue(1)
        self.panel.stage_label.setText("Done.")

    def _open_folder(self) -> None:
        if self.state.instance_dir is not None:
            api.open_folder(self.state.instance_dir)

    def on_leave(self) -> bool:
        return False
