"""Shared base class for wizard pages."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ..state import WizardState


class WizardPage(QWidget):
    """One page of the wizard.

    `ready_changed(bool)` gates the window's Next button. `custom_action(str)` is a
    small event bus for actions that are not "advance to the next page" (starting the
    run, jumping to the Manage screen, ...) -- the window listens for specific strings.

    `busy_changed(bool)` marks this page as running a cancellable background operation
    (a `create`/`add` pipeline run, a Manage-tab action): the window uses it to
    maintain one busy state for the whole window (blocking navigation, warning on a
    second attempt to start something, confirming before closing) -- see
    `WizardWindow._set_busy`. Only pages that actually launch an `EngineWorker` need
    to emit it; the default here is simply never emitting it (never busy).
    """

    ready_changed = Signal(bool)
    custom_action = Signal(str)
    busy_changed = Signal(bool)

    title = "Page"

    def __init__(self, state: WizardState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self._ready = True

    def set_ready(self, ready: bool) -> None:
        if ready != self._ready:
            self._ready = ready
            self.ready_changed.emit(ready)

    def is_ready(self) -> bool:
        return self._ready

    def on_enter(self) -> None:
        """Called every time the page becomes current."""

    def on_leave(self) -> bool:
        """Called when Next is pressed; write into `self.state` here. False blocks."""
        return True

    def request_cancel(self) -> None:
        """Cancel whatever background operation this page owns, if any -- called by
        the window when the user chooses to quit while busy. Default: no-op, for
        pages that never go busy."""
