"""GUI smoke tests: every page constructs without error, and the Qt reporter bridge
correctly turns `Reporter` calls into Qt signals (incl. cancellation).

Runs headless (`QT_QPA_PLATFORM=offscreen`, set before PySide6 is imported anywhere
in the process) so it needs no display -- this is a widget-construction/signal-wiring
check, not a rendering check.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from collections2wabbajack.api import OperationCancelled
from collections2wabbajack.gui.pages.collection import CollectionPage
from collections2wabbajack.gui.pages.display import DisplayPage
from collections2wabbajack.gui.pages.home import HomePage
from collections2wabbajack.gui.pages.location import LocationPage
from collections2wabbajack.gui.pages.manage import ManagePage
from collections2wabbajack.gui.pages.progress import ProgressPage
from collections2wabbajack.gui.pages.review import ReviewPage
from collections2wabbajack.gui.pages.signin import SignInPage
from collections2wabbajack.gui.pages.tools_page import ToolsPage
from collections2wabbajack.gui.reporter_bridge import QtReporter
from collections2wabbajack.gui.state import WizardState

PAGE_CLASSES = [
    SignInPage,
    HomePage,
    CollectionPage,
    LocationPage,
    ToolsPage,
    DisplayPage,
    ReviewPage,
    ProgressPage,
    ManagePage,
]


@pytest.fixture(autouse=True)
def _clean_keyring(monkeypatch):
    # SignInPage.__init__ reads the real OS keyring to prefill the key field; stub it
    # out so the test suite never touches the machine's actual credential store.
    from collections2wabbajack import api

    monkeypatch.setattr(api, "get_saved_api_key", lambda: None)


@pytest.mark.parametrize("page_cls", PAGE_CLASSES)
def test_page_constructs(qtbot, page_cls):
    # Construction only, deliberately not on_enter(): LocationPage's would spin up a
    # real background QThread (disk size scan) and hit the registry/network, which
    # doesn't belong in a smoke test and can outlive the widget past teardown.
    state = WizardState()
    page = page_cls(state)
    qtbot.addWidget(page)
    assert page.title  # just: no exception constructing it


def test_wizard_window_constructs(qtbot):
    from collections2wabbajack.gui.app import WizardWindow

    window = WizardWindow()
    qtbot.addWidget(window)
    assert window._current == "signin"
    assert set(window.pages) == {
        "signin",
        "home",
        "collection",
        "location",
        "tools",
        "display",
        "review",
        "progress",
        "manage",
    }


# -- QtReporter bridge --------------------------------------------------------------------


def test_qt_reporter_emits_signals(qtbot):
    reporter = QtReporter()
    with qtbot.waitSignal(reporter.stageStarted, timeout=1000) as blocker:
        reporter.stage("fetch", 10)
    assert blocker.args == ["fetch", 10]

    with qtbot.waitSignal(reporter.progressed, timeout=1000) as blocker:
        reporter.progress(3, 10, "some-file.7z")
    assert blocker.args == [3, 10, "some-file.7z"]

    with qtbot.waitSignal(reporter.logged, timeout=1000) as blocker:
        reporter.log("a log line")
    assert blocker.args == ["a log line"]

    with qtbot.waitSignal(reporter.warned, timeout=1000) as blocker:
        reporter.warn("a warning")
    assert blocker.args == ["a warning"]

    with qtbot.waitSignal(reporter.stageDone, timeout=1000) as blocker:
        reporter.done("fetch", "ok")
    assert blocker.args == ["fetch", "ok"]


def test_qt_reporter_cancel_raises_between_stages():
    reporter = QtReporter()
    reporter.stage("first")  # fine before cancel
    reporter.cancel()
    with pytest.raises(OperationCancelled):
        reporter.stage("second")
    with pytest.raises(OperationCancelled):
        reporter.progress(1, 10)


def test_qt_reporter_reset_clears_cancel():
    reporter = QtReporter()
    reporter.cancel()
    assert reporter.is_cancelled() is True
    reporter.reset()
    assert reporter.is_cancelled() is False
    reporter.stage("ok-again")  # does not raise
