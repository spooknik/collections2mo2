"""GUI smoke tests: every page constructs without error, and the Qt reporter bridge
correctly turns `Reporter` calls into Qt signals (incl. cancellation).

Runs headless (`QT_QPA_PLATFORM=offscreen`, set before PySide6 is imported anywhere
in the process) so it needs no display -- this is a widget-construction/signal-wiring
check, not a rendering check.
"""

from __future__ import annotations

import os
from pathlib import Path

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


# -- theme: muted text must stay legible on a dark palette --------------------------------
# (a real bug: `color: palette(mid);` renders close-to-invisible dark-grey-on-dark-grey --
# see `gui/theme.py`'s docstring for the measured contrast ratios).


def _relative_luminance(color) -> float:
    def chan(v: int) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * chan(color.red()) + 0.7152 * chan(color.green()) + 0.0722 * chan(color.blue())


def _dark_palette():
    from PySide6.QtGui import QColor, QPalette

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(240, 240, 240))
    pal.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    pal.setColor(QPalette.ColorRole.Text, QColor(240, 240, 240))
    pal.setColor(QPalette.ColorRole.Mid, QColor(88, 88, 88))  # the old, broken choice
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(150, 150, 150))
    return pal


@pytest.mark.parametrize("label_name", ["current_item_label", "rate_label"])
def test_progress_widget_muted_labels_readable_on_dark_palette(qtbot, label_name):
    """Renders a ProgressWidget under an explicit dark palette and checks the muted
    labels' *painted pixels* -- not just the palette role -- are meaningfully lighter
    than the window background, i.e. actually legible rather than merely "not the
    exact same colour"."""
    widget = ProgressWidget()
    qtbot.addWidget(widget)
    widget.setPalette(_dark_palette())
    widget.resize(320, 240)

    label = getattr(widget, label_name)
    label.setText("Elapsed 0:05 -- some status text")
    widget.show()
    qtbot.waitExposed(widget)

    img = label.grab().toImage()
    bg_luminance = _relative_luminance(widget.palette().color(widget.backgroundRole()))
    lightest = max(
        (img.pixelColor(x, y) for y in range(img.height()) for x in range(img.width())),
        key=_relative_luminance,
    )
    text_luminance = _relative_luminance(lightest)

    # `palette(mid)` (#585858) against this Window (#2d2d2d) is a luminance delta of
    # ~0.02 -- nearly invisible. `palette(placeholder-text)` (#969696) is ~0.19 lighter.
    assert text_luminance - bg_luminance > 0.08


def test_warning_style_picks_a_theme_appropriate_amber(qtbot):
    from PySide6.QtWidgets import QLabel

    from collections2wabbajack.gui.theme import is_dark_palette, warning_style

    label = QLabel()
    qtbot.addWidget(label)

    label.setPalette(_dark_palette())
    assert is_dark_palette(label) is True
    dark_style = warning_style(label)

    light_pal = _dark_palette()
    from PySide6.QtGui import QColor, QPalette

    light_pal.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    label.setPalette(light_pal)
    assert is_dark_palette(label) is False
    light_style = warning_style(label)

    assert dark_style != light_style  # theme-appropriate, not one fixed hex for both


# -- launch flow: Home-first when signed in, Sign-in otherwise, Back-to-start ------------
# -- recents persistence -----------------------------------------------------------------


@pytest.fixture
def _isolated_qsettings(tmp_path):
    """Point every `QSettings(...)` construction (recents.py's `_settings()` included)
    at a throwaway INI file under `tmp_path` instead of the real per-user store, so
    the test suite never touches the machine's actual registry/settings."""
    from PySide6.QtCore import QSettings

    prev_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    yield tmp_path
    QSettings.setDefaultFormat(prev_format)


def test_home_shown_first_when_signed_in(qtbot, monkeypatch, _isolated_qsettings):
    """A saved key must show Home immediately, never blocking on the background
    validation that follows."""
    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: "fake-key")
    monkeypatch.setattr(
        api, "validate_api_key", lambda api_key: api.SignInResult(name="Bob", is_premium=True)
    )

    window = WizardWindow()
    qtbot.addWidget(window)

    # Home is current the instant the window is built -- before the worker thread
    # (started at the end of __init__) has had any chance to report back.
    assert window._current == "home"
    assert window._checking_signin is True
    assert window.account_label.text() == "checking..."

    assert window._signin_worker is not None
    qtbot.waitSignal(window._signin_worker.finished, timeout=2000)
    qtbot.wait(50)  # let the queued succeeded/failed signal reach the GUI thread

    assert window._checking_signin is False
    assert window.state.signin is not None
    assert window.state.signin.name == "Bob"
    assert window._current == "home"  # validation succeeding never navigates away


def test_signin_shown_when_no_key(qtbot, monkeypatch, _isolated_qsettings):
    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: None)

    window = WizardWindow()
    qtbot.addWidget(window)

    assert window._current == "signin"
    assert window._signin_worker is None  # nothing to validate


def test_signin_shown_when_saved_key_fails_validation(qtbot, monkeypatch, _isolated_qsettings):
    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: "bad-key")

    def _fail(api_key):
        raise api.ApiError("Nexus rejected that key.")

    monkeypatch.setattr(api, "validate_api_key", _fail)

    window = WizardWindow()
    qtbot.addWidget(window)
    assert window._current == "home"  # still shown immediately

    qtbot.waitSignal(window._signin_worker.failed, timeout=2000)
    qtbot.wait(50)

    assert window._current == "signin"
    assert window.state.signin is None
    assert "rejected" in window.pages["signin"].status_label.text()


def test_back_to_start_resets_state_keeps_account(qtbot, monkeypatch, _isolated_qsettings):
    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: None)
    window = WizardWindow()
    qtbot.addWidget(window)

    window.state.api_key = "abc123"
    window.state.signin = api.SignInResult(name="Bob", is_premium=True)
    window.state.collection_url = (
        "https://www.nexusmods.com/games/skyrimspecialedition/collections/xyz"
    )
    window.state.instance_dir = Path("C:/somewhere")
    window.state.game_path = Path("C:/games/skyrim")
    window.state.tool_ids = ["loot", "nemesis"]
    window.state.run_succeeded = True

    old_collection_page = window.pages["collection"]
    window._on_custom_action("reset:home")

    assert window._current == "home"
    # the account survives...
    assert window.state.api_key == "abc123"
    assert window.state.signin is not None
    assert window.state.signin.name == "Bob"
    # ...but every choice made for the run is cleared
    assert window.state.collection_url == ""
    assert window.state.instance_dir is None
    assert window.state.game_path is None
    assert window.state.tool_ids == []
    assert window.state.run_succeeded is None
    # and the linear-flow pages were rebuilt, not just left with stale widget state
    assert window.pages["collection"] is not old_collection_page


def test_recents_persist_and_prune_invalid(_isolated_qsettings):
    from collections2wabbajack.gui import recents

    tmp_path = _isolated_qsettings
    valid_dir = tmp_path / "instance"
    valid_dir.mkdir()
    (valid_dir / "c2wj-instance.json").write_text("{}")
    missing_dir = tmp_path / "gone"  # never created -- simulates a deleted instance

    recents.save_recents(
        [
            recents.RecentInstance(str(valid_dir), "GTS", "2026-01-01T00:00:00+00:00"),
            recents.RecentInstance(str(missing_dir), "Old", "2025-01-01T00:00:00+00:00"),
        ]
    )

    loaded = recents.load_recents()
    assert [r.path for r in loaded] == [str(valid_dir)]  # the missing one was pruned

    recents.remember_instance(valid_dir, "GTS Renamed")
    loaded2 = recents.load_recents()
    assert loaded2[0].path == str(valid_dir)
    assert loaded2[0].collection_name == "GTS Renamed"


def test_manage_page_auto_loads_most_recent_valid_instance(qtbot, monkeypatch, _isolated_qsettings):
    from collections2wabbajack import api
    from collections2wabbajack.gui import recents
    from collections2wabbajack.gui.pages.manage import ManagePage
    from collections2wabbajack.gui.state import WizardState

    tmp_path = _isolated_qsettings
    valid_dir = tmp_path / "instance"
    valid_dir.mkdir()
    (valid_dir / "c2wj-instance.json").write_text("{}")
    recents.remember_instance(valid_dir, "GTS")

    loaded_paths = []

    def _fake_load(instance_dir):
        loaded_paths.append(str(instance_dir))
        # Auto-load only needs to prove it *tried* to load the right path -- avoid
        # constructing a full InstanceSummary here by failing the call cleanly.
        raise api.ApiError("stubbed for this test")

    monkeypatch.setattr(api, "load_instance", _fake_load)

    page = ManagePage(WizardState())
    qtbot.addWidget(page)
    page.on_enter()

    assert page.path_edit.text() == str(valid_dir)
    assert page._load_worker is not None
    qtbot.waitSignal(page._load_worker.failed, timeout=2000)
    qtbot.wait(50)
    assert loaded_paths == [str(valid_dir)]


# -- idle/busy progress state, mutual exclusion, close-while-busy -----------------------
# (product-owner-reported bugs: the progress widget looked "active" before any operation
# started and its elapsed timer never stopped; two operations could run concurrently and
# silently share one log/progress widget; the window could be closed mid-operation.)


def test_progress_widget_idle_on_construction(qtbot):
    """Fresh widget, page just loaded, no operation started yet: determinate 0%, not
    spinning, and the tick timer is a no-op (no elapsed line)."""
    widget = ProgressWidget()
    qtbot.addWidget(widget)

    assert widget.progress_bar.maximum() != 0  # determinate, not indeterminate/animating
    assert widget.progress_bar.value() == 0
    assert widget.rate_label.text() == ""
    assert widget._operation_running is False

    # A tick landing before any operation starts must not conjure up an elapsed line.
    widget._on_tick()
    assert widget.rate_label.text() == ""


def test_progress_widget_finish_stops_and_freezes_elapsed(qtbot):
    """`reset()` starts the elapsed tick; `finish()` (operation done/failed/cancelled)
    must stop it and freeze whatever value was last shown -- not keep counting up."""
    widget = ProgressWidget()
    qtbot.addWidget(widget)
    reporter = QtReporter()
    widget.attach(reporter)

    widget.reset()
    reporter.stage("fetch")
    reporter._flush()
    assert widget._operation_running is True

    widget._stage_started_at -= 5  # simulate 5s having elapsed, without a real sleep
    widget._on_tick()
    running_text = widget.rate_label.text()
    assert running_text.startswith("Elapsed")

    widget.finish()
    assert widget._operation_running is False
    assert widget.progress_bar.maximum() != 0  # no longer left spinning

    # Further ticks (as the real 1s QTimer would still deliver) must not advance it.
    widget._stage_started_at -= 5
    widget._on_tick()
    assert widget.rate_label.text() == running_text


def test_progress_widget_reset_with_operation_name_keeps_log(qtbot):
    """Manage-tab actions reuse one widget across several operations -- the log must
    grow a timestamped separator, not be wiped, so earlier output stays readable."""
    widget = ProgressWidget()
    qtbot.addWidget(widget)
    widget.log_message("first operation's output")

    widget.reset("Install tools")

    text = widget.log_view.toPlainText()
    assert "first operation's output" in text  # not cleared
    assert "Install tools" in text  # separator names the new operation


def test_manage_page_busy_state_blocks_buttons_and_second_operation(qtbot, monkeypatch):
    """Starting one action disables the other action buttons (and Load/Browse/
    Back-to-start) and shows the busy label; a second attempt while busy is refused
    rather than started as a concurrent worker; completion re-enables everything."""
    import threading

    from PySide6.QtWidgets import QMessageBox

    from collections2wabbajack import api
    from collections2wabbajack.gui.pages.manage import ManagePage
    from collections2wabbajack.gui.state import WizardState

    release = threading.Event()

    def fake_add(instance_dir, url, reporter=None):
        release.wait(5)
        return 0

    monkeypatch.setattr(api, "add_collection_layer", fake_add)
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append("warned"))

    page = ManagePage(WizardState())
    qtbot.addWidget(page)
    page._instance = Path("C:/fake/instance")

    busy_events: list[bool] = []
    page.busy_changed.connect(busy_events.append)

    page._run_action(
        "Add collection",
        api.add_collection_layer,
        {"instance_dir": page._instance, "url": "x"},
        lambda rc: None,
    )

    assert page._busy is True
    assert page.add_btn.isEnabled() is False
    assert page.remove_btn.isEnabled() is False
    assert page.update_btn.isEnabled() is False
    assert page.tools_btn.isEnabled() is False
    assert page.wabbajack_btn.isEnabled() is False
    assert page.load_btn.isEnabled() is False
    assert page.browse_btn.isEnabled() is False
    assert page.back_to_start_btn.isEnabled() is False
    assert page.cancel_btn.isEnabled() is True
    assert page.busy_label.isHidden() is False  # shown -- isVisible() needs page.show()
    assert busy_events == [True]

    first_worker = page._action_worker
    page._add_layer()  # a second attempt while busy: refused, not a second worker
    assert page._action_worker is first_worker
    assert warnings == ["warned"]

    release.set()
    qtbot.waitSignal(first_worker.finished, timeout=2000)
    qtbot.wait(50)

    assert page._busy is False
    assert page.add_btn.isEnabled() is True
    assert page.load_btn.isEnabled() is True
    assert page.cancel_btn.isEnabled() is False
    assert page.busy_label.isHidden() is True
    assert busy_events == [True, False]


def test_window_close_while_busy_prompts_and_can_be_declined(qtbot, monkeypatch):
    """Closing the window while an operation is running must confirm first; declining
    must leave the window open and the operation running."""
    import threading

    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: None)
    window = WizardWindow()
    qtbot.addWidget(window)

    release = threading.Event()

    def fake_export(instance_dir, reporter=None):
        release.wait(5)
        return 0

    monkeypatch.setattr(api, "export_to_wabbajack", fake_export)
    manage = window.pages["manage"]
    manage._instance = Path("C:/fake/instance")
    manage._run_action(
        "Export to Wabbajack",
        api.export_to_wabbajack,
        {"instance_dir": manage._instance},
        lambda rc: None,
    )
    assert window._busy is True

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() is False  # declined -- window stays open
    assert window._busy is True  # operation was left running

    release.set()
    worker = manage._action_worker
    qtbot.waitSignal(worker.finished, timeout=2000)
    qtbot.wait(50)
    assert window._busy is False


def test_window_close_while_busy_confirmed_cancels_and_accepts(qtbot, monkeypatch):
    """Confirming the close-while-busy prompt cancels the in-flight operation (via
    `request_cancel`) and lets the window close."""
    import threading

    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox

    from collections2wabbajack import api
    from collections2wabbajack.gui.app import WizardWindow

    monkeypatch.setattr(api, "get_saved_api_key", lambda: None)
    window = WizardWindow()
    qtbot.addWidget(window)

    def fake_export(instance_dir, reporter=None):
        from collections2wabbajack.api import OperationCancelled

        while not reporter.is_cancelled():
            threading.Event().wait(0.05)
        raise OperationCancelled("cancelled")

    monkeypatch.setattr(api, "export_to_wabbajack", fake_export)
    manage = window.pages["manage"]
    manage._instance = Path("C:/fake/instance")
    # In real use the window can only be busy while Manage (or Progress) is the
    # current page, since navigation is blocked while busy -- fake that here since
    # this test drives the worker directly rather than through a button click.
    window._current = "manage"
    manage._run_action(
        "Export to Wabbajack",
        api.export_to_wabbajack,
        {"instance_dir": manage._instance},
        lambda rc: None,
    )
    assert window._busy is True
    worker = manage._action_worker

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() is True

    qtbot.waitSignal(worker.finished, timeout=2000)
    qtbot.wait(50)
    assert window._busy is False
