"""Page 3: paste a collection URL, fetch its metadata, optionally survey FOMODs."""

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
        self._survey_worker: EngineWorker | None = None

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

        survey_box = QGroupBox("Optional pre-flight")
        survey_layout = QVBoxLayout(survey_box)
        survey_layout.addWidget(
            QLabel(
                "Checks which mods without a recorded FOMOD install ship an installer "
                "anyway. Uses part of Nexus's hourly API budget and is skipped by default; "
                "it never blocks Continue."
            )
        )
        self.survey_btn = QPushButton("Check FOMODs")
        self.survey_btn.clicked.connect(self._run_survey)
        survey_layout.addWidget(self.survey_btn)
        self.survey_status = QLabel("")
        self.survey_status.setWordWrap(True)
        survey_layout.addWidget(self.survey_status)
        survey_box.setEnabled(False)
        self.survey_box = survey_box
        layout.addWidget(survey_box)

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
        self.survey_box.setEnabled(False)
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
            self.revision_combo.addItem(f"revision {choice.revision_number}", choice.revision_number)
        idx = self.revision_combo.findData(summary.revision_number)
        self.revision_combo.setCurrentIndex(max(idx, 0))
        self.revision_combo.blockSignals(False)
        self.state.selected_revision = self.revision_combo.currentData()
        self.info_box.setVisible(True)
        self.survey_box.setEnabled(True)
        self.set_ready(True)

    def _on_fetch_failed(self, message: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.status_label.setText(message)
        self.set_ready(False)

    def _on_revision_changed(self, _index: int) -> None:
        self.state.selected_revision = self.revision_combo.currentData()

    def _run_survey(self) -> None:
        if self.state.collection_summary is None:
            return
        self.survey_btn.setEnabled(False)
        self.survey_status.setText("Surveying (this uses part of Nexus's hourly API budget)...")
        self._survey_worker = EngineWorker(
            api.run_fomod_survey,
            {
                "url": self.state.collection_url,
                "revision": self.state.selected_revision,
                "api_key": self.state.api_key,
                "jobs": self.state.jobs,
            },
        )
        self._survey_worker.succeeded.connect(self._on_survey_done)
        self._survey_worker.failed.connect(self._on_survey_failed)
        self._survey_worker.start()

    def _on_survey_done(self, summary: api.SurveySummary) -> None:
        self.survey_btn.setEnabled(True)
        self.state.survey_summary = summary
        if summary.status == "rate_limited":
            self.survey_status.setText(
                f"{summary.detail} ({summary.fetched}/{summary.targets} checked so far, "
                f"{summary.fresh_fomod_count} found with a FOMOD installer)."
            )
        elif summary.status == "ok":
            self.survey_status.setText(
                f"Checked {summary.fetched}/{summary.targets} mods -- "
                f"{summary.fresh_fomod_count} have a FOMOD installer not recorded in the "
                "collection (defaults will be used for those)."
            )
        else:
            self.survey_status.setText(f"Survey did not complete: {summary.detail}")

    def _on_survey_failed(self, message: str) -> None:
        self.survey_btn.setEnabled(True)
        self.survey_status.setText(message)

    def on_leave(self) -> bool:
        return self.state.collection_summary is not None
