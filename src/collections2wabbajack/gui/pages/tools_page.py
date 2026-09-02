"""Page 5: optional modding tools checklist, grouped by catalogue group."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QScrollArea, QVBoxLayout, QWidget

from ... import api
from ..theme import MUTED_STYLE
from .base import WizardPage


class ToolsPage(WizardPage):
    title = "Tools"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        outer = QVBoxLayout(self)
        outer.addWidget(
            QLabel(
                "Optional modding tools, installed into <code>Tools\\</code> in the instance "
                "and registered as MO2 executables. Defaults are pre-ticked."
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._inner_layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self._checkboxes: dict[str, QCheckBox] = {}
        for group_name, entries in api.list_tool_groups():
            box = QGroupBox(group_name)
            box_layout = QVBoxLayout(box)
            for entry in entries:
                cb = QCheckBox(entry.name)
                if entry.size_hint_mb:
                    cb.setText(f"{entry.name}  (~{entry.size_hint_mb} MB)")
                if entry.disabled:
                    cb.setEnabled(False)
                    cb.setChecked(False)
                    note = entry.note or "not yet installable"
                    box_layout.addWidget(cb)
                    note_label = QLabel(f"    {note}")
                    note_label.setWordWrap(True)
                    note_label.setStyleSheet(MUTED_STYLE)
                    box_layout.addWidget(note_label)
                else:
                    cb.setChecked(entry.default)
                    box_layout.addWidget(cb)
                    if entry.requires:
                        req_label = QLabel(f"    requires: {entry.requires}")
                        req_label.setWordWrap(True)
                        req_label.setStyleSheet(MUTED_STYLE)
                        box_layout.addWidget(req_label)
                self._checkboxes[entry.id] = cb
            self._inner_layout.addWidget(box)
        self._inner_layout.addStretch(1)

    def on_leave(self) -> bool:
        self.state.tool_ids = [
            tool_id for tool_id, cb in self._checkboxes.items() if cb.isEnabled() and cb.isChecked()
        ]
        return True
