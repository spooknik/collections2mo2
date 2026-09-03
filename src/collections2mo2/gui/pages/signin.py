"""Page 1: sign in with a Nexus personal API key."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ... import api
from ..worker import EngineWorker
from .base import WizardPage


class SignInPage(WizardPage):
    title = "Sign in"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._worker: EngineWorker | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "collections2mo2 downloads mod files straight from Nexus Mods on your "
            "behalf, using your account's personal API key. Automatic downloads require a "
            "<b>Nexus Premium</b> membership -- without Premium, Nexus will not issue the "
            "direct download links this tool needs.\n\n"
            "Your key is stored only on this PC (in Windows Credential Manager), never sent "
            "anywhere except to Nexus Mods itself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        get_key_btn = QPushButton("Get my key from Nexus Mods...")
        get_key_btn.clicked.connect(self._open_key_page)
        layout.addWidget(get_key_btn)

        layout.addWidget(QLabel("Paste your personal API key:"))
        row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("paste key here")
        self.key_edit.textChanged.connect(self._on_text_changed)
        row.addWidget(self.key_edit)
        self.show_btn = QPushButton("Show")
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._toggle_show)
        row.addWidget(self.show_btn)
        layout.addLayout(row)

        btn_row = QHBoxLayout()
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.clicked.connect(self._validate)
        btn_row.addWidget(self.continue_btn)
        self.signout_btn = QPushButton("Sign out")
        self.signout_btn.clicked.connect(self._sign_out)
        self.signout_btn.setVisible(False)
        btn_row.addWidget(self.signout_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        saved = api.get_saved_api_key()
        if saved:
            self.key_edit.setText(saved)

        self.set_ready(False)

    def on_enter(self) -> None:
        # Reached via the header's "Account"/"Change key" button after an already
        # -successful sign-in (startup validation, or an earlier visit here): reflect
        # that instead of showing a blank status until Continue is pressed again.
        if self.state.signin is not None and self.key_edit.text().strip() == self.state.api_key:
            premium = "Premium" if self.state.signin.is_premium else "not Premium"
            self.status_label.setText(f"Signed in as {self.state.signin.name} ({premium}).")
            self.signout_btn.setVisible(True)
            self.set_ready(True)

    def show_error(self, message: str) -> None:
        """Called by the window when a saved key failed background validation at
        startup, redirecting here."""
        self.status_label.setText(message)
        self.signout_btn.setVisible(False)
        self.set_ready(False)

    def _open_key_page(self) -> None:
        QDesktopServices.openUrl(QUrl(api.nexus_api_key_signup_url()))

    def _toggle_show(self, checked: bool) -> None:
        self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_text_changed(self) -> None:
        if self.state.signin is not None:
            # Key changed after a successful sign-in: it must be re-validated.
            self.state.signin = None
            self.set_ready(False)
            self.signout_btn.setVisible(False)

    def _validate(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            self.status_label.setText("Paste your API key first.")
            return
        self.continue_btn.setEnabled(False)
        self.status_label.setText("Checking with Nexus Mods...")
        self._worker = EngineWorker(api.validate_api_key, {"api_key": key})
        self._worker.succeeded.connect(self._on_validated)
        self._worker.failed.connect(self._on_validate_failed)
        self._worker.start()

    def _on_validated(self, result: api.SignInResult) -> None:
        self.continue_btn.setEnabled(True)
        self.state.api_key = self.key_edit.text().strip()
        self.state.signin = result
        api.activate_api_key(self.state.api_key)
        api.save_api_key(self.state.api_key)
        premium = "Premium" if result.is_premium else "not Premium"
        self.status_label.setText(f"Signed in as {result.name} ({premium}).")
        if not result.is_premium:
            self.status_label.setText(
                self.status_label.text()
                + " Automatic mod downloads need Nexus Premium; you can still browse "
                "collections, but `create` will fail on the download step."
            )
        self.signout_btn.setVisible(True)
        self.set_ready(True)

    def _on_validate_failed(self, message: str) -> None:
        self.continue_btn.setEnabled(True)
        self.status_label.setText(message)
        self.set_ready(False)

    def _sign_out(self) -> None:
        api.clear_api_key()
        self.key_edit.clear()
        self.state.api_key = ""
        self.state.signin = None
        self.status_label.setText("Signed out.")
        self.signout_btn.setVisible(False)
        self.set_ready(False)

    def on_leave(self) -> bool:
        if self.state.signin is None:
            QMessageBox.warning(self, "Sign in required", "Validate your API key first.")
            return False
        return True
