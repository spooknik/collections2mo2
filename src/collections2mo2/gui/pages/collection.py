"""Page 3: paste a collection URL, fetch its metadata, pick a revision."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ... import api
from ..worker import EngineWorker
from .base import WizardPage


class CollectionPage(WizardPage):
    title = "Collection"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._fetch_worker: EngineWorker | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Nexus Mods collection URL:"))
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "https://www.nexusmods.com/games/skyrimspecialedition/collections/xxxxxx"
        )
        row.addWidget(self.url_edit)
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.clicked.connect(self._fetch)
        row.addWidget(self.fetch_btn)
        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        info_box = QGroupBox("Collection")
        info_layout = QVBoxLayout(info_box)
        self.name_label = QLabel("")
        self.name_label.setWordWrap(True)
        self.stats_label = QLabel("")
        info_layout.addWidget(self.name_label)
        info_layout.addWidget(self.stats_label)
        rev_row = QHBoxLayout()
        rev_row.addWidget(QLabel("Revision:"))
        self.revision_combo = QComboBox()
        self.revision_combo.currentIndexChanged.connect(self._on_revision_changed)
        rev_row.addWidget(self.revision_combo)
        rev_row.addStretch(1)
        info_layout.addLayout(rev_row)
        info_box.setVisible(False)
        self.info_box = info_box
        layout.addWidget(info_box)

        layout.addStretch(1)
        self.set_ready(False)

    def on_enter(self) -> None:
        if self.state.collection_url:
            self.url_edit.setText(self.state.collection_url)

    def _fetch(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.status_label.setText("Paste a collection URL first.")
            return
        self.fetch_btn.setEnabled(False)
        self.info_box.setVisible(False)
        self.status_label.setText("Fetching collection metadata...")
        self._fetch_worker = EngineWorker(
            api.fetch_collection_summary, {"url": url, "api_key": self.state.api_key or None}
        )
        self._fetch_worker.succeeded.connect(self._on_fetched)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        self._fetch_worker.start()

    def _on_fetched(self, summary: api.CollectionSummary) -> None:
        self.fetch_btn.setEnabled(True)
        self.state.collection_url = self.url_edit.text().strip()
        self.state.collection_summary = summary
        self.status_label.setText("")
        self.name_label.setText(f"<b>{summary.name}</b> by {summary.author}")
        self.stats_label.setText(
            f"{summary.mod_count} mods, {api.format_bytes(summary.total_size)}, "
            f"latest revision {summary.latest_revision_number}"
        )
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        for choice in api.list_revisions(summary):
            self.revision_combo.addItem(
                f"revision {choice.revision_number}", choice.revision_number
            )
        idx = self.revision_combo.findData(summary.revision_number)
        self.revision_combo.setCurrentIndex(max(idx, 0))
        self.revision_combo.blockSignals(False)
        self.state.selected_revision = self.revision_combo.currentData()
        self.info_box.setVisible(True)
        self.set_ready(True)

    def _on_fetch_failed(self, message: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.status_label.setText(message)
        self.set_ready(False)

    def _on_revision_changed(self, _index: int) -> None:
        self.state.selected_revision = self.revision_combo.currentData()

    def on_leave(self) -> bool:
        return self.state.collection_summary is not None
