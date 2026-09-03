"""Page 7: review every choice before starting the run."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout

from ... import api
from .base import WizardPage


class ReviewPage(WizardPage):
    title = "Review & run"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.clicked.connect(lambda: self.custom_action.emit("start_run"))
        layout.addWidget(self.start_btn)
        layout.addStretch(1)
        self.set_ready(False)  # advancing happens via Start, not Next

    def on_enter(self) -> None:
        s = self.state
        summary = s.collection_summary
        lines = ["<h3>Review</h3>"]
        if s.signin:
            premium = "Premium" if s.signin.is_premium else "not Premium"
            lines.append(f"<b>Signed in as:</b> {s.signin.name} ({premium})")
        if summary:
            lines.append(
                f"<b>Collection:</b> {summary.name} by {summary.author} -- "
                f"revision {s.selected_revision} ({summary.mod_count} mods, "
                f"{api.format_bytes(summary.total_size)})"
            )
        reused = " (existing folder, downloads reused)" if s.preset_instance_dir else ""
        lines.append(f"<b>Instance folder:</b> {s.instance_dir}{reused}")
        lines.append(f"<b>Game folder:</b> {s.game_path}")
        lines.append(f"<b>Copy game into instance:</b> {'yes' if s.stock_game else 'no'}")
        lines.append(f"<b>Tools:</b> {', '.join(s.tool_ids) if s.tool_ids else '(none)'}")
        lines.append(
            f"<b>Display:</b> resolution={s.resolution}, vsync={s.vsync}, window={s.window}"
        )
        self.summary_label.setText("<br>".join(lines))

    def on_leave(self) -> bool:
        return False
