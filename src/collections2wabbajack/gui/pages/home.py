"""Page 2: choose "new instance" vs "manage an existing one"."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout

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

        layout.addStretch(1)
        self.set_ready(False)  # this page only advances via the buttons above

    def on_leave(self) -> bool:
        return False
