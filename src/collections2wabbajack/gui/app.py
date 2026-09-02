"""`c2wj-gui` entry point: the wizard window."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

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


class WizardWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("collections2wabbajack")
        self.resize(920, 720)

        self.state = WizardState()
        self.pages: dict[str, WizardPage] = {}
        self._history: list[str] = []
        self._current = "signin"

        self.stack = QStackedWidget()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
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

        self.stack.setCurrentWidget(self.pages["signin"])
        self.pages["signin"].on_enter()
        self._update_nav()

    def _add_page(self, name: str, page: WizardPage) -> None:
        self.pages[name] = page
        self.stack.addWidget(page)
        page.ready_changed.connect(self._update_nav)
        page.custom_action.connect(self._on_custom_action)

    # -- navigation ----------------------------------------------------------------

    def _update_nav(self, *_args) -> None:
        page = self.pages[self._current]
        is_progress = self._current == "progress"
        self.nav_bar.setVisible(not is_progress)
        self.back_btn.setEnabled(bool(self._history))
        self.next_btn.setVisible(self._current not in NO_NEXT)
        self.next_btn.setEnabled(page.is_ready())
        self.setWindowTitle(f"collections2wabbajack -- {page.title}")

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


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = WizardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
