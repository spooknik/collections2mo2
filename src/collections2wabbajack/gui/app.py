"""`c2wj-gui` entry point: the wizard window."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import api
from .pages.base import WizardPage
from .pages.collection import CollectionPage
from .pages.display import DisplayPage
from .pages.home import HomePage
from .pages.location import LocationPage
from .pages.manage import ManagePage
from .pages.progress import ProgressPage
from .pages.review import ReviewPage
from .pages.signin import SignInPage
from .pages.tools_page import ToolsPage
from .state import WizardState
from .theme import MUTED_STYLE
from .worker import EngineWorker

# Pages reachable by linear Next/Back once you commit to the "create a new instance"
# path. "home" and "manage" are reached/left only via custom_action jumps (see
# `_on_custom_action`), not by falling off the end of this list.
LINEAR_ORDER = [
    "signin",
    "home",
    "collection",
    "location",
    "tools",
    "display",
    "review",
    "progress",
]

# Pages that advance some other way than the shared Next button (their own button,
# or they are a dead end): Next is hidden for these.
NO_NEXT = {"home", "review", "progress", "manage"}

# Rebuilt from scratch by "Back to start" (`_reset_wizard`) so no widget keeps stale
# text/selections from the previous run -- "home", "signin" and "manage" carry their
# own state (or none) and are left alone.
RESETTABLE_PAGES = ["collection", "location", "tools", "display", "review", "progress"]


class WizardWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("collections2wabbajack")
        self.resize(920, 720)

        self.state = WizardState()
        self.pages: dict[str, WizardPage] = {}
        self._history: list[str] = []
        self._current = "signin"
        self._checking_signin = False
        self._signin_worker: EngineWorker | None = None

        self.stack = QStackedWidget()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)

        self.header_bar = QWidget()
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(0, 0, 0, 8)
        self.account_label = QLabel("")
        self.account_label.setStyleSheet(MUTED_STYLE)
        header_layout.addWidget(self.account_label)
        header_layout.addStretch(1)
        self.account_btn = QPushButton("Account")
        self.account_btn.clicked.connect(lambda: self._navigate_to("signin", push_history=True))
        header_layout.addWidget(self.account_btn)
        outer.addWidget(self.header_bar)

        outer.addWidget(self.stack, 1)

        self.nav_bar = QWidget()
        nav_layout = QHBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self._go_back)
        nav_layout.addWidget(self.back_btn)
        nav_layout.addStretch(1)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self._go_next)
        nav_layout.addWidget(self.next_btn)
        outer.addWidget(self.nav_bar)

        self.setCentralWidget(central)

        for name, page in (
            ("signin", SignInPage(self.state)),
            ("home", HomePage(self.state)),
            ("collection", CollectionPage(self.state)),
            ("location", LocationPage(self.state)),
            ("tools", ToolsPage(self.state)),
            ("display", DisplayPage(self.state)),
            ("review", ReviewPage(self.state)),
            ("progress", ProgressPage(self.state)),
            ("manage", ManagePage(self.state)),
        ):
            self._add_page(name, page)

        # Sign-in is only the first thing shown when there is no saved key to try --
        # otherwise Home shows immediately and the key is validated in the background
        # (see `_start_signin_check`), never blocking the window on the network.
        saved_key = api.get_saved_api_key()
        if saved_key:
            self.state.api_key = saved_key
            self._current = "home"
            self._checking_signin = True
        else:
            self._current = "signin"

        self.stack.setCurrentWidget(self.pages[self._current])
        self.pages[self._current].on_enter()
        self._update_nav()

        if saved_key:
            self._start_signin_check(saved_key)

    def _add_page(self, name: str, page: WizardPage) -> None:
        self.pages[name] = page
        self.stack.addWidget(page)
        page.ready_changed.connect(self._update_nav)
        page.custom_action.connect(self._on_custom_action)

    # -- sign-in: validate a saved key in the background, without blocking Home -----

    def _start_signin_check(self, api_key: str) -> None:
        self._signin_worker = EngineWorker(api.validate_api_key, {"api_key": api_key})
        self._signin_worker.succeeded.connect(self._on_signin_check_ok)
        self._signin_worker.failed.connect(self._on_signin_check_failed)
        self._signin_worker.start()

    def _on_signin_check_ok(self, result: api.SignInResult) -> None:
        self._checking_signin = False
        self.state.signin = result
        api.activate_api_key(self.state.api_key)
        self._update_nav()

    def _on_signin_check_failed(self, message: str) -> None:
        self._checking_signin = False
        self.state.signin = None
        self.pages["signin"].show_error(message)
        self._navigate_to("signin", push_history=True)

    # -- navigation ----------------------------------------------------------------

    def _update_account_bar(self) -> None:
        if self._checking_signin:
            self.account_label.setText("checking...")
        elif self.state.signin is not None:
            premium = "Premium" if self.state.signin.is_premium else "not Premium"
            self.account_label.setText(f"Signed in as {self.state.signin.name} ({premium})")
        else:
            self.account_label.setText("Not signed in")
        self.account_btn.setText("Change key" if self.state.signin is not None else "Sign in")
        self.header_bar.setVisible(self._current != "signin")

    def _update_nav(self, *_args) -> None:
        page = self.pages[self._current]
        is_progress = self._current == "progress"
        self.nav_bar.setVisible(not is_progress)
        self.back_btn.setEnabled(bool(self._history))
        self.next_btn.setVisible(self._current not in NO_NEXT)
        self.next_btn.setEnabled(page.is_ready())
        self.setWindowTitle(f"collections2wabbajack -- {page.title}")
        self._update_account_bar()

    def _navigate_to(self, name: str, *, push_history: bool) -> None:
        if push_history:
            self._history.append(self._current)
        self._current = name
        self.stack.setCurrentWidget(self.pages[name])
        self.pages[name].on_enter()
        self._update_nav()

    def _go_next(self) -> None:
        page = self.pages[self._current]
        if not page.on_leave():
            return
        idx = LINEAR_ORDER.index(self._current)
        self._navigate_to(LINEAR_ORDER[idx + 1], push_history=True)

    def _go_back(self) -> None:
        if not self._history:
            return
        prev = self._history.pop()
        self._current = prev
        self.stack.setCurrentWidget(self.pages[prev])
        self.pages[prev].on_enter()
        self._update_nav()

    def _on_custom_action(self, action: str) -> None:
        if action.startswith("goto:"):
            self._navigate_to(action.split(":", 1)[1], push_history=True)
        elif action == "start_run":
            self._navigate_to("progress", push_history=True)
            self.pages["progress"].start()
        elif action == "reset:home":
            self._reset_wizard()
        elif action.startswith("manage:"):
            path = action.split(":", 1)[1]
            self.pages["manage"].load_path(path)
            self._navigate_to("manage", push_history=True)

    def _reset_wizard(self) -> None:
        """"Back to start": clear the collection/location/tools/display choices and
        any run result, keeping the signed-in account, then rebuild the linear-flow
        pages so no widget (a typed URL, a chosen path, a checked box) keeps stale
        state from the previous run."""
        self.state.reset_for_new_run()
        for name in RESETTABLE_PAGES:
            old = self.pages[name]
            idx = self.stack.indexOf(old)
            new_page = type(old)(self.state)
            self.stack.removeWidget(old)
            old.deleteLater()
            self.pages[name] = new_page
            new_page.ready_changed.connect(self._update_nav)
            new_page.custom_action.connect(self._on_custom_action)
            self.stack.insertWidget(idx, new_page)
        self._history.clear()
        self._current = "home"
        self.stack.setCurrentWidget(self.pages["home"])
        self.pages["home"].on_enter()
        self._update_nav()


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = WizardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
