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

from ... import profile
from ..theme import MUTED_STYLE
from .base import WizardPage


class DisplayPage(WizardPage):
    title = "Display settings"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "<b>Keep</b> uses your own <code>My Games</code> INIs as-is -- or, if the "
                "collection ships its own display settings (SSE Display Tweaks), those. "
                "<b>Auto-detect</b> reads this PC's primary monitor. Anything else is applied "
                "as an explicit override."
            )
        )
        note = QLabel(
            "If a collection ships SSE Display Tweaks, it overrides SkyrimPrefs.ini's display "
            "settings entirely -- so when you choose anything other than Keep here, c2wj also "
            "writes a small override mod on top of it, so your choice still applies."
        )
        note.setWordWrap(True)
        note.setStyleSheet(MUTED_STYLE)
        layout.addWidget(note)

        res_box = QGroupBox("Resolution")
        res_layout = QVBoxLayout(res_box)
        self.res_group = QButtonGroup(self)
        self.res_keep = QRadioButton("Keep (recommended)")
        self._detected = profile._detect_resolution()
        auto_label = (
            f"Auto-detect ({self._detected[0]}x{self._detected[1]})"
            if self._detected
            else "Auto-detect from this PC's primary monitor"
        )
        self.res_auto = QRadioButton(auto_label)
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
