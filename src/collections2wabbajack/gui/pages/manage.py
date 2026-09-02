"""Manage tab: work with an instance `create` already built.

Pick an instance folder, see each collection layer with its installed and latest
Nexus revision, add another collection as a layer, remove one, install more tools,
and (once `wabbajack.py` exists) export to Wabbajack.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ... import api
from .. import recents
from ..progress_widget import ProgressWidget
from ..reporter_bridge import QtReporter
from ..worker import EngineWorker
from .base import WizardPage


class _AddLayerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a collection")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collection URL (an add-on that assumes the base is installed):"))
        self.url_edit = QLineEdit()
        layout.addWidget(self.url_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def url(self) -> str:
        return self.url_edit.text().strip()


class ManagePage(WizardPage):
    title = "Manage instance"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._load_worker: EngineWorker | None = None
        self._action_worker: EngineWorker | None = None
        self._instance: Path | None = None
        self._summary: api.InstanceSummary | None = None

        layout = QVBoxLayout(self)

        back_btn = QPushButton("<- Back to start")
        back_btn.clicked.connect(lambda: self.custom_action.emit("reset:home"))
        layout.addWidget(back_btn)

        recent_row = QHBoxLayout()
        recent_row.addWidget(QLabel("Recent:"))
        self.recent_combo = QComboBox()
        self.recent_combo.currentIndexChanged.connect(self._on_recent_selected)
        recent_row.addWidget(self.recent_combo, 1)
        layout.addLayout(recent_row)

        pick_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("path to an existing c2wj instance")
        pick_row.addWidget(self.path_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        pick_row.addWidget(browse_btn)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load)
        pick_row.addWidget(load_btn)
        layout.addLayout(pick_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.info_box = QGroupBox("Instance")
        info_layout = QVBoxLayout(self.info_box)
        self.info_label = QLabel("")
        info_layout.addWidget(self.info_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Layer", "Installed", "Latest", "Mods", "Update?"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        info_layout.addWidget(self.table)

        action_row = QHBoxLayout()
        add_btn = QPushButton("Add collection...")
        add_btn.clicked.connect(self._add_layer)
        action_row.addWidget(add_btn)
        remove_btn = QPushButton("Remove selected layer")
        remove_btn.clicked.connect(self._remove_layer)
        action_row.addWidget(remove_btn)
        self.update_btn = QPushButton("Update selected layer to latest")
        self.update_btn.clicked.connect(self._update_layer)
        action_row.addWidget(self.update_btn)
        tools_btn = QPushButton("Install more tools...")
        tools_btn.clicked.connect(self._install_tools)
        action_row.addWidget(tools_btn)
        self.wabbajack_btn = QPushButton("Export to Wabbajack")
        self.wabbajack_btn.clicked.connect(self._export_wabbajack)
        action_row.addWidget(self.wabbajack_btn)
        action_row.addStretch(1)
        info_layout.addLayout(action_row)

        # Same widget the Progress page uses for a full `create` run -- every
        # Manage-tab action (add/remove/update a layer, install tools, export to
        # Wabbajack) drives one `QtReporter`, so this gets the same stage line,
        # counter, current item and rate/elapsed line for free.
        self.action_progress = ProgressWidget()
        self.action_progress.setMinimumHeight(180)
        info_layout.addWidget(self.action_progress, 1)

        self.info_box.setVisible(False)
        layout.addWidget(self.info_box, 1)
        self.set_ready(False)

    def on_enter(self) -> None:
        if not api.has_wabbajack_support():
            self.wabbajack_btn.setEnabled(False)
            self.wabbajack_btn.setToolTip("Coming soon: wabbajack.py has not landed yet.")
        if not api.has_update_support():
            self.update_btn.setEnabled(False)
            self.update_btn.setToolTip("Coming soon: update.py has not landed yet.")
        self._refresh_recent_combo()
        # First time this page is shown this session and nothing has been picked yet
        # (including by a Home-page recent-instance button, which sets the path field
        # before we get here): auto-load the most recently opened valid instance.
        if self._instance is None and not self.path_edit.text().strip():
            recent = recents.most_recent_valid()
            if recent is not None:
                self.load_path(recent.path)

    def load_path(self, path: str) -> None:
        """Set the instance path and load it -- used by Home's recent-instance
        buttons and by auto-loading the most recent instance on entry."""
        self.path_edit.setText(path)
        self._load()

    def _refresh_recent_combo(self) -> None:
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("(choose a recent instance)", "")
        for r in recents.load_recents():
            label = f"{r.collection_name or Path(r.path).name} -- {r.path}"
            self.recent_combo.addItem(label, r.path)
        self.recent_combo.blockSignals(False)

    def _on_recent_selected(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        if path:
            self.load_path(path)

    def _browse(self) -> None:
        start = self.path_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose instance folder", start)
        if chosen:
            self.path_edit.setText(chosen)

    def _load(self) -> None:
        text = self.path_edit.text().strip()
        if not text:
            self.status_label.setText("Choose an instance folder first.")
            return
        self.status_label.setText("Loading...")
        self._load_worker = EngineWorker(api.load_instance, {"instance_dir": text})
        self._load_worker.succeeded.connect(self._on_loaded)
        self._load_worker.failed.connect(self._on_load_failed)
        self._load_worker.start()

    def _on_loaded(self, summary: api.InstanceSummary) -> None:
        self._instance = summary.out
        self._summary = summary
        base_name = summary.layers[0].name if summary.layers else summary.out.name
        recents.remember_instance(summary.out, base_name)
        self._refresh_recent_combo()
        self.status_label.setText("")
        self.info_label.setText(
            f"<b>{summary.out}</b> -- {summary.game_name or summary.game_domain}, "
            f"MO2 {summary.mo2_version}, {summary.user_mod_count} user mod(s)"
        )
        self.table.setRowCount(len(summary.layers))
        for row, layer in enumerate(summary.layers):
            installed = f"{layer.revision}" + (" (base)" if layer.is_base else "")
            latest = str(layer.latest_revision_number) if layer.latest_revision_number else "?"
            update = "yes" if layer.update_available else ""
            values = [
                f"{layer.name} ({layer.slug})",
                installed,
                latest,
                str(layer.mod_count),
                update,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(1000, layer.slug)  # stash the slug for later lookup
                self.table.setItem(row, col, item)
        self.info_box.setVisible(True)

    def _on_load_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self.info_box.setVisible(False)

    def _append_log(self, text: str, level: str = "info") -> None:
        self.action_progress.log_message(text, level=level)

    def _make_reporter(self) -> QtReporter:
        reporter = QtReporter()
        self.action_progress.reset()
        self.action_progress.attach(reporter)
        return reporter

    def _add_layer(self) -> None:
        if self._instance is None:
            self.status_label.setText("Load an instance first.")
            return
        dialog = _AddLayerDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url = dialog.url()
        if not url:
            return
        reporter = self._make_reporter()
        self._action_worker = EngineWorker(
            api.add_collection_layer,
            {"instance_dir": self._instance, "url": url},
            reporter=reporter,
        )
        self._action_worker.succeeded.connect(lambda rc: self._on_action_done("add", rc))
        self._action_worker.failed.connect(lambda m: self._append_log(m, level="error"))
        self._action_worker.start()

    def _remove_layer(self) -> None:
        if self._instance is None:
            return
        row = self.table.currentRow()
        if row < 0:
            self.status_label.setText("Select a layer to remove first.")
            return
        slug = self.table.item(row, 0).data(1000)
        is_base = self._summary is not None and self._summary.layers[row].is_base
        confirm = QMessageBox.question(
            self,
            "Remove layer",
            f"Remove layer {slug}? Mod folders it alone owns will be deleted."
            + (" This is the BASE layer." if is_base else ""),
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        reporter = self._make_reporter()
        self._action_worker = EngineWorker(
            api.remove_collection_layer,
            {"instance_dir": self._instance, "slug": slug, "force": is_base},
            reporter=reporter,
        )
        self._action_worker.succeeded.connect(lambda rc: self._on_action_done("remove", rc))
        self._action_worker.failed.connect(lambda m: self._append_log(m, level="error"))
        self._action_worker.start()

    def _update_layer(self) -> None:
        if self._instance is None:
            self.status_label.setText("Load an instance first.")
            return
        if not api.has_update_support():
            QMessageBox.information(self, "Coming soon", "update.py is not available yet.")
            return
        row = self.table.currentRow()
        if row < 0:
            self.status_label.setText("Select a layer to update first.")
            return
        slug = self.table.item(row, 0).data(1000)
        confirm = QMessageBox.question(
            self,
            "Update layer",
            f"Update {slug} to its latest published revision? Only the delta is "
            "re-downloaded/installed; mods you changed by hand are flagged, not "
            "silently overwritten.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        reporter = self._make_reporter()
        self._action_worker = EngineWorker(
            api.update_collection_layer,
            {"instance_dir": self._instance, "slug": slug, "to": "latest"},
            reporter=reporter,
        )
        self._action_worker.succeeded.connect(lambda rc: self._on_action_done("update", rc))
        self._action_worker.failed.connect(lambda m: self._append_log(m, level="error"))
        self._action_worker.start()

    def _on_action_done(self, action: str, rc: int) -> None:
        self._append_log(f"{action}: {'ok' if rc == 0 else 'failed, see log above'}")
        if self._instance is not None:
            self._load()

    def _install_tools(self) -> None:
        if self._instance is None:
            self.status_label.setText("Load an instance first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Install more tools")
        layout = QVBoxLayout(dialog)
        checkboxes: dict[str, QCheckBox] = {}
        for group_name, entries in api.list_tool_groups(self._instance):
            box = QGroupBox(group_name)
            box_layout = QVBoxLayout(box)
            for entry in entries:
                cb = QCheckBox(f"{entry.name} ({entry.status})")
                cb.setEnabled(not entry.disabled and entry.status != "installed")
                checkboxes[entry.id] = cb
                box_layout.addWidget(cb)
            layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ids = [tool_id for tool_id, cb in checkboxes.items() if cb.isChecked()]
        if not ids:
            return
        reporter = self._make_reporter()
        self._action_worker = EngineWorker(
            api.install_more_tools,
            {"instance_dir": self._instance, "tool_ids": ids},
            reporter=reporter,
        )
        self._action_worker.succeeded.connect(lambda ok: self._append_log("tools: done" if ok else "tools: failed"))
        self._action_worker.failed.connect(lambda m: self._append_log(m, level="error"))
        self._action_worker.start()

    def _export_wabbajack(self) -> None:
        if self._instance is None:
            return
        if not api.has_wabbajack_support():
            QMessageBox.information(self, "Coming soon", "Wabbajack export is not available yet.")
            return
        reporter = self._make_reporter()
        self._action_worker = EngineWorker(
            api.export_to_wabbajack, {"instance_dir": self._instance}, reporter=reporter
        )
        self._action_worker.succeeded.connect(lambda rc: self._append_log("wabbajack: done" if rc == 0 else "wabbajack: failed"))
        self._action_worker.failed.connect(lambda m: self._append_log(m, level="error"))
        self._action_worker.start()

    def on_leave(self) -> bool:
        return False
