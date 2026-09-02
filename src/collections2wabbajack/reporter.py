"""Progress reporting interface shared by every pipeline stage.

The stages used to `print` straight to stdout. They still do for most of their
detail output, but everything that is *progress* or a *stage summary* now goes
through a `Reporter`, so a future GUI can implement the same five calls and drive
the pipeline without capturing stdout:

    stage(name, total)      a stage is starting
    progress(done, total)   one unit of that stage finished
    log / warn(msg)         a line of detail / a problem worth surfacing
    done(name, summary)     the stage finished

`ConsoleReporter` reproduces the previous terminal output (progress redraws on a
single line); `NullReporter` swallows everything, which is what tests and any
embedding process want.
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Reporter(Protocol):
    """What a pipeline stage may tell the outside world about its progress."""

    def stage(self, name: str, total: int | None = None) -> None:
        """A stage called `name` is starting, with `total` units of work if known."""

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        """`done` of `total` units finished; `label` describes the unit just finished."""

    def log(self, msg: str) -> None:
        """A line of ordinary detail."""

    def warn(self, msg: str) -> None:
        """Something the user should see but that does not stop the stage."""

    def done(self, name: str, summary: str = "") -> None:
        """The stage called `name` finished; `summary` is a one-line result."""


class NullReporter:
    """A `Reporter` that discards everything."""

    def stage(self, name: str, total: int | None = None) -> None:
        return None

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        return None

    def log(self, msg: str) -> None:
        return None

    def warn(self, msg: str) -> None:
        return None

    def done(self, name: str, summary: str = "") -> None:
        return None


class ConsoleReporter:
    """Prints to the terminal: stage banners, one-line progress, warnings to stderr."""

    def __init__(self, stream=None, err=None, one_line: bool | None = None):
        self._out = stream if stream is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        # Rewriting a single line only works on a terminal; when the output is being
        # piped to a file, print one line per unit instead so nothing is lost.
        if one_line is None:
            one_line = bool(getattr(self._out, "isatty", lambda: False)())
        self._one_line = one_line
        self._pending = False

    # -- internals ---------------------------------------------------------------

    def _finish_line(self) -> None:
        if self._pending:
            print(file=self._out)
            self._pending = False

    def _write(self, text: str) -> None:
        self._finish_line()
        print(text, file=self._out, flush=True)

    # -- Reporter ----------------------------------------------------------------

    def stage(self, name: str, total: int | None = None) -> None:
        suffix = f" ({total})" if total is not None else ""
        self._write(f"\n== {name}{suffix}")

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        counter = f"[{done}/{total}]" if total is not None else f"[{done}]"
        line = f"{counter} {label}".rstrip()
        if not self._one_line:
            self._write(line)
            return
        # Pad to overwrite whatever was longer on the previous redraw.
        print(f"\r{line:<100.100}", end="", file=self._out, flush=True)
        self._pending = True
        if total is not None and done >= total:
            self._finish_line()

    def log(self, msg: str) -> None:
        self._write(msg)

    def warn(self, msg: str) -> None:
        self._finish_line()
        print(f"warning: {msg}", file=self._err, flush=True)

    def done(self, name: str, summary: str = "") -> None:
        self._write(f"-- {name}: {summary}" if summary else f"-- {name}")


def get_reporter(reporter: Reporter | None) -> Reporter:
    """Every stage's `reporter=None` default: fall back to the console."""
    return reporter if reporter is not None else ConsoleReporter()
