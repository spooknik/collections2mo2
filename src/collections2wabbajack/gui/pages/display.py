"""Page 6: resolution, vsync, window mode -- mapped to `profile`'s display flags."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from .base import WizardPage


class DisplayPage(WizardPage):
    title = "Display settings"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "The profile's INIs start from your own <code>My Games</code> INIs (plus any "
                "tweaks the collection itself specifies), so 'Keep' is a safe default -- these "
                "settings only override what's already there."
            )
        )

        res_box = QGroupBox("Resolution")
        res_layout = QVBoxLayout(res_box)
        self.res_group = QButtonGroup(self)
        self.res_keep = QRadioButton("Keep (recommended)")
        self.res_auto = QRadioButton("Auto-detect from this PC's primary monitor")
        self.res_custom = QRadioButton("Custom:")
        self.res_keep.setChecked(True)
        for i, btn in enumerate((self.res_keep, self.res_auto, self.res_custom)):
            self.res_group.addButton(btn, i)
            res_layout.addWidget(btn)
        custom_row = QHBoxLayout()
        custom_row.addSpacing(20)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(640, 7680)
        self.width_spin.setValue(1920)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(480, 4320)
        self.height_spin.setValue(1080)
        custom_row.addWidget(self.width_spin)
        custom_row.addWidget(QLabel("x"))
        custom_row.addWidget(self.height_spin)
        custom_row.addStretch(1)
        res_layout.addLayout(custom_row)
        layout.addWidget(res_box)

        row = QHBoxLayout()
        row.addWidget(QLabel("VSync:"))
        self.vsync_combo = QComboBox()
        self.vsync_combo.addItem("Keep", "keep")
        self.vsync_combo.addItem("On", "on")
        self.vsync_combo.addItem("Off", "off")
        row.addWidget(self.vsync_combo)
        row.addSpacing(20)
        row.addWidget(QLabel("Window mode:"))
        self.window_combo = QComboBox()
        self.window_combo.addItem("Keep", "keep")
        self.window_combo.addItem("Fullscreen", "fullscreen")
        self.window_combo.addItem("Borderless", "borderless")
        self.window_combo.addItem("Windowed", "windowed")
        row.addWidget(self.window_combo)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(1)

    def on_leave(self) -> bool:
        if self.res_keep.isChecked():
            self.state.resolution = "keep"
        elif self.res_auto.isChecked():
            self.state.resolution = "auto"
        else:
            self.state.resolution = f"{self.width_spin.value()}x{self.height_spin.value()}"
        self.state.vsync = self.vsync_combo.currentData()
        self.state.window = self.window_combo.currentData()
        return True
