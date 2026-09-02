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
from collections2wabbajack.gui.progress_widget import ProgressWidget
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
    assert blocker.args == ["fetch", 10, None, None]

    # progress() is throttled (see below) -- flush synchronously so the assertion
    # doesn't depend on the real QTimer firing within the wait window.
    with qtbot.waitSignal(reporter.progressed, timeout=1000) as blocker:
        reporter.progress(3, 10, "some-file.7z", bytes_done=1234, bytes_total=5678)
        reporter._flush()
    assert blocker.args == [3, 10, "some-file.7z", 1234, 5678]

    with qtbot.waitSignal(reporter.logged, timeout=1000) as blocker:
        reporter.log("a log line")
    assert blocker.args == ["a log line"]

    with qtbot.waitSignal(reporter.warned, timeout=1000) as blocker:
        reporter.warn("a warning")
    assert blocker.args == ["a warning"]

    with qtbot.waitSignal(reporter.stageDone, timeout=1000) as blocker:
        reporter.done("fetch", "ok")
    assert blocker.args == ["fetch", "ok"]


def test_qt_reporter_throttles_rapid_progress(qtbot):
    """292 progress() calls in a tight loop must not become 292 Qt signal emissions --
    only the most recent call survives until the next flush."""
    reporter = QtReporter()
    received: list[tuple] = []
    reporter.progressed.connect(lambda *args: received.append(args))

    for i in range(292):
        reporter.progress(i, 292, f"file-{i}.7z", bytes_done=i * 1000, bytes_total=292000)

    assert received == []  # nothing flushed yet -- still coalesced
    reporter._flush()
    assert len(received) == 1
    assert received[0][0] == 291  # only the latest call's data survives
    assert received[0][2] == "file-291.7z"

    # A second flush with nothing new pending emits nothing further.
    reporter._flush()
    assert len(received) == 1


def test_qt_reporter_stage_and_done_flush_pending_progress(qtbot):
    """stage()/done() must not leave a stale progress reading buffered behind a
    stage boundary."""
    reporter = QtReporter()
    received: list[tuple] = []
    reporter.progressed.connect(lambda *args: received.append(args))

    reporter.progress(5, 10, "mid-file.7z")
    assert received == []
    reporter.done("download", "ok")
    assert len(received) == 1
    assert received[0][:3] == (5, 10, "mid-file.7z")


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


# -- ProgressWidget: a scripted reporter feed through the whole page ------------------


def test_progress_widget_scripted_download_sequence(qtbot):
    """Feeds a scripted `Reporter` call sequence (as `downloader.run_download` would
    make it) through a real `QtReporter` into `ProgressWidget` and checks the labels
    end up showing what a person watching the page should see."""
    widget = ProgressWidget()
    qtbot.addWidget(widget)
    reporter = QtReporter()
    widget.attach(reporter)

    reporter.stage("download", 34)
    reporter._flush()
    assert widget.stage_label.text() == "Stage 3 of 7 - Downloading archives"
    assert widget.progress_bar.maximum() == 34

    total_bytes = 340_000_000
    for i in range(1, 4):
        reporter.progress(
            i,
            34,
            f"ok       Some Long Mod Name {i}.7z  10.0 MB",
            bytes_done=i * 10_000_000,
            bytes_total=total_bytes,
        )
        reporter._flush()

    assert widget.counter_label.text() == "3 / 34 files"
    assert "Some Long Mod Name 3.7z" in widget.current_item_label.text()
    assert "of" in widget.rate_label.text()  # "... of 340.00 MB" once bytes_total is known

    reporter.warn("archive X.7z is missing an md5 in the manifest")
    reporter.log("downloading 34 mod(s) for SkyrimSE (skyrimspecialedition) with 4 worker(s)")
    reporter.done("download", "done in 12.3s: ok=34")

    log_html = widget.log_view.toHtml()
    assert "archive X.7z is missing an md5" in log_html
    assert "#d9a441" in log_html  # the warning is tinted amber
    assert "-- download: done in 12.3s: ok=34" in widget.log_view.toPlainText()


def test_progress_widget_elides_long_current_item(qtbot):
    widget = ProgressWidget()
    qtbot.addWidget(widget)
    widget.resize(300, 400)
    reporter = QtReporter()
    widget.attach(reporter)

    reporter.stage("install", 5)
    reporter._flush()
    long_name = "A" * 40 + "Really Long Mod Folder Name That Should Get Elided" + "B" * 40
    reporter.progress(2, 5, long_name)
    reporter._flush()

    shown = widget.current_item_label.text()
    assert shown != long_name  # it was elided
    assert shown.startswith("A")
    assert shown.endswith("B")
    assert "..." in shown or "…" in shown
