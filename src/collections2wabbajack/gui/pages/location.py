"""Page 4: install location and game folder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ... import api
from ..theme import warning_style
from ..worker import EngineWorker
from .base import WizardPage


class LocationPage(WizardPage):
    title = "Install location and game"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._size_worker: EngineWorker | None = None
        self._defaulted = False

        layout = QVBoxLayout(self)

        inst_box = QGroupBox("Instance folder")
        inst_layout = QVBoxLayout(inst_box)
        row = QHBoxLayout()
        self.instance_edit = QLineEdit()
        self.instance_edit.textChanged.connect(self._on_instance_changed)
        row.addWidget(self.instance_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_instance)
        row.addWidget(browse_btn)
        inst_layout.addLayout(row)
        self.instance_warning = QLabel("")
        self.instance_warning.setWordWrap(True)
        self.instance_warning.setStyleSheet(warning_style(self.instance_warning))
        inst_layout.addWidget(self.instance_warning)
        layout.addWidget(inst_box)

        game_box = QGroupBox("Game folder (Skyrim Special Edition)")
        game_layout = QVBoxLayout(game_box)
        row2 = QHBoxLayout()
        self.game_edit = QLineEdit()
        self.game_edit.textChanged.connect(self._on_game_changed)
        row2.addWidget(self.game_edit)
        browse_game_btn = QPushButton("Browse...")
        browse_game_btn.clicked.connect(self._browse_game)
        row2.addWidget(browse_game_btn)
        game_layout.addLayout(row2)
        self.game_status = QLabel("")
        self.game_status.setWordWrap(True)
        game_layout.addWidget(self.game_status)

        self.stock_check = QCheckBox("Copy the game into the instance (recommended)")
        self.stock_check.setChecked(True)
        self.stock_check.toggled.connect(self._on_stock_toggled)
        game_layout.addWidget(self.stock_check)
        stock_note = QLabel(
            "Keeps every patcher (LOD generation, Nemesis/Pandora, xEdit patches) off your "
            "real Steam install -- nothing about the collection can ever corrupt it."
        )
        stock_note.setWordWrap(True)
        game_layout.addWidget(stock_note)

        self.space_label = QLabel("")
        self.space_label.setWordWrap(True)
        game_layout.addWidget(self.space_label)
        layout.addWidget(game_box)

        layout.addStretch(1)

    def on_enter(self) -> None:
        if not self._defaulted and self.state.collection_summary is not None:
            self._defaulted = True
            default_dir = api.default_instance_dir(self.state.collection_summary.name)
            self.instance_edit.setText(str(default_dir))
        if not self.game_edit.text():
            detected = api.detect_skyrim_se_path()
            if detected is not None:
                self.game_edit.setText(str(detected))
        self._update_space()
        self._validate()

    def _browse_instance(self) -> None:
        start = self.instance_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose instance folder", start)
        if chosen:
            self.instance_edit.setText(chosen)

    def _browse_game(self) -> None:
        start = self.game_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose Skyrim Special Edition folder", start)
        if chosen:
            self.game_edit.setText(chosen)

    def _on_instance_changed(self) -> None:
        text = self.instance_edit.text().strip()
        if not text:
            self.instance_warning.setText("")
            self.set_ready(False)
            return
        warnings = api.path_warnings(text)
        self.instance_warning.setText("\n".join(warnings))
        self._update_space()
        self._validate()

    def _on_game_changed(self) -> None:
        path = Path(self.game_edit.text().strip()) if self.game_edit.text().strip() else None
        if path is not None and path.is_dir():
            self.game_status.setText(f"found: {path}")
            self._start_size_lookup(path)
        elif path is not None:
            self.game_status.setText("that folder does not exist")
        else:
            self.game_status.setText("")
        self._validate()

    def _on_stock_toggled(self, _checked: bool) -> None:
        self._update_space()

    def _start_size_lookup(self, path: Path) -> None:
        self._size_worker = EngineWorker(api.dir_size_bytes, {"path": path})
        self._size_worker.succeeded.connect(self._on_size_done)
        self._size_worker.start()

    def _on_size_done(self, size: int) -> None:
        self._game_size = size
        self._update_space()

    def _update_space(self) -> None:
        inst = self.instance_edit.text().strip()
        if not inst:
            self.space_label.setText("")
            return
        try:
            free = api.disk_free_bytes(inst)
        except OSError:
            self.space_label.setText("")
            return
        text = f"free space at target: {api.format_bytes(free)}"
        size = getattr(self, "_game_size", None)
        if self.stock_check.isChecked() and size:
            text += f"  |  game copy needed: ~{api.format_bytes(size)}"
            if size > free:
                text = f"WARNING: not enough free space. {text}"
        self.space_label.setText(text)

    def _validate(self) -> None:
        inst_ok = bool(self.instance_edit.text().strip())
        game_path = self.game_edit.text().strip()
        game_ok = bool(game_path) and Path(game_path).is_dir()
        self.set_ready(inst_ok and game_ok)

    def on_leave(self) -> bool:
        if not self.is_ready():
            return False
        self.state.instance_dir = Path(self.instance_edit.text().strip())
        self.state.game_path = Path(self.game_edit.text().strip())
        self.state.stock_game = self.stock_check.isChecked()
        return True
