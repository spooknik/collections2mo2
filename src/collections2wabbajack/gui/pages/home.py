"""Page 2: choose "new instance" vs "manage an existing one", plus quick-jump buttons
for recently opened instances (`..recents`)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QGroupBox, QLabel, QPushButton, QVBoxLayout

from .. import recents
from .base import WizardPage


class HomePage(WizardPage):
    title = "What would you like to do?"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>What would you like to do?</h2>"))

        new_btn = QPushButton("Create a new instance from a Nexus collection")
        new_btn.setMinimumHeight(48)
        new_btn.clicked.connect(lambda: self.custom_action.emit("goto:collection"))
        layout.addWidget(new_btn)

        manage_btn = QPushButton("Manage an existing instance")
        manage_btn.setMinimumHeight(48)
        manage_btn.clicked.connect(lambda: self.custom_action.emit("goto:manage"))
        layout.addWidget(manage_btn)

        self.recents_box = QGroupBox("Recent instances")
        self.recents_layout = QVBoxLayout(self.recents_box)
        self.recents_box.setVisible(False)
        layout.addWidget(self.recents_box)

        layout.addStretch(1)
        self.set_ready(False)  # this page only advances via the buttons above

    def on_enter(self) -> None:
        self._refresh_recents()

    def _refresh_recents(self) -> None:
        while self.recents_layout.count():
            item = self.recents_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        entries = recents.load_recents()
        self.recents_box.setVisible(bool(entries))
        for entry in entries:
            name = entry.collection_name or Path(entry.path).name
            btn = QPushButton(f"Open {name} at {entry.path}")
            btn.clicked.connect(
                lambda _checked=False, p=entry.path: self.custom_action.emit(f"manage:{p}")
            )
            self.recents_layout.addWidget(btn)

    def on_leave(self) -> bool:
        return False
