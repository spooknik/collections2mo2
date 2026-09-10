"""Page 1: sign in to Nexus Mods through the browser (OAuth 2.0).

Nexus's API Acceptable Use Policy forbids a public app from asking for the user's
*personal* API key, so there is no key field here any more: the button hands the
sign-in to Nexus's own authorise page in the system browser (`api.sign_in`, which
blocks on a loopback redirect for up to ten minutes) and we only ever see the token
it hands back, stored in Windows Credential Manager by `oauth.py`.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ... import api
from ..theme import MUTED_STYLE
from ..worker import EngineWorker
from .base import WizardPage

NOT_PREMIUM_NOTE = (
    " Automatic mod downloads need Nexus Premium; you can still browse "
    "collections, but `create` will fail on the download step."
)

# Sign-in workers still running when their page is destroyed (the wizard rebuilds
# pages on "Back to start", and tests drop them at teardown). A QThread whose Python
# wrapper is garbage-collected mid-run takes the process with it, so the worker is
# kept alive here until it actually finishes.
_LIVE_WORKERS: set[EngineWorker] = set()


class SignInPage(WizardPage):
    title = "Sign in"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self._worker: EngineWorker | None = None
        self._cancel_event: threading.Event | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "collections2mo2 downloads mod files straight from Nexus Mods on your "
            "behalf. It connects through Nexus's official sign-in (OAuth) in your "
            "browser, so this app never sees your password.\n\n"
            "Automatic downloads require a <b>Nexus Premium</b> membership -- without "
            "Premium, Nexus will not issue the direct download links this tool needs."
            "\n\n"
            "The sign-in token Nexus hands back is stored only on this PC (in Windows "
            "Credential Manager), never sent anywhere except to Nexus Mods itself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        btn_row = QHBoxLayout()
        self.signin_btn = QPushButton("Sign in with Nexus Mods")
        self.signin_btn.setDefault(True)
        self.signin_btn.clicked.connect(self._sign_in)
        btn_row.addWidget(self.signin_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_sign_in)
        self.cancel_btn.setVisible(False)
        btn_row.addWidget(self.cancel_btn)
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

        self.manage_btn = QPushButton("Manage access on nexusmods.com")
        self.manage_btn.setFlat(True)
        self.manage_btn.setStyleSheet(MUTED_STYLE)
        self.manage_btn.clicked.connect(self._open_authorized_apps)
        manage_row = QHBoxLayout()
        manage_row.addWidget(self.manage_btn)
        manage_row.addStretch(1)
        layout.addLayout(manage_row)

        self.set_ready(False)

    # -- entering / leaving --------------------------------------------------------

    def on_enter(self) -> None:
        # Reached via the header's "Account" button after an already-successful
        # sign-in (the startup check, or an earlier visit here): reflect that instead
        # of showing a blank status until the button is pressed again.
        if self.state.signin is not None:
            self._show_signed_in(self.state.signin)

    def on_leave(self) -> bool:
        if self.state.signin is None:
            QMessageBox.warning(self, "Sign in required", "Sign in with Nexus Mods first.")
            return False
        return True

    def show_error(self, message: str) -> None:
        """Called by the window when the saved sign-in failed its background check at
        startup, redirecting here."""
        self.status_label.setText(message)
        self.signout_btn.setVisible(False)
        self.set_ready(False)

    def request_cancel(self) -> None:
        self._cancel_sign_in()

    # -- sign in / out -------------------------------------------------------------

    def _sign_in(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._cancel_event = threading.Event()
        self.signin_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.signout_btn.setVisible(False)
        self.status_label.setText("Waiting for you to approve collections2mo2 in your browser...")
        worker = EngineWorker(api.sign_in, {"cancel": self._cancel_event})
        self._worker = worker
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(lambda: _LIVE_WORKERS.discard(worker))
        worker.succeeded.connect(self._on_signed_in)
        worker.failed.connect(self._on_sign_in_failed)
        worker.cancelled.connect(self._on_sign_in_cancelled)
        worker.start()

    def _cancel_sign_in(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_btn.setEnabled(False)

    def _reset_buttons(self) -> None:
        self.signin_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)

    def _show_signed_in(self, result: api.SignInResult) -> None:
        premium = "Premium" if result.is_premium else "not Premium"
        text = f"Signed in as {result.name} ({premium})."
        if not result.is_premium:
            text += NOT_PREMIUM_NOTE
        self.status_label.setText(text)
        self.signout_btn.setVisible(True)
        self.set_ready(True)

    def _on_signed_in(self, result: api.SignInResult) -> None:
        self._reset_buttons()
        self.state.signin = result
        self._show_signed_in(result)

    def _on_sign_in_failed(self, message: str) -> None:
        self._reset_buttons()
        self.state.signin = None
        self.status_label.setText(message)
        self.signout_btn.setVisible(False)
        self.set_ready(False)

    def _on_sign_in_cancelled(self) -> None:
        self._reset_buttons()
        self.status_label.setText("Sign-in cancelled.")
        self.set_ready(self.state.signin is not None)

    def _sign_out(self) -> None:
        api.sign_out()
        self.state.signin = None
        self.status_label.setText("Signed out.")
        self.signout_btn.setVisible(False)
        self.set_ready(False)

    def _open_authorized_apps(self) -> None:
        QDesktopServices.openUrl(QUrl(api.nexus_authorized_apps_url()))
