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

`stage()` and `progress()` also take optional keyword-only extras, additive so every
existing caller and implementation keeps working unchanged:

    stage(name, total, *, stage_index=None, stage_count=None)
    progress(done, total, label, *, bytes_done=None, bytes_total=None)

`stage_index`/`stage_count` let a caller that owns the whole pipeline's stage order
say "this is stage 3 of 7"; nothing in this repo populates them yet (the GUI derives
a stage's position itself from a known stage-name sequence -- see
`gui/progress_widget.py`), but the hook is here for whoever wires up the orchestrator
next. `bytes_done`/`bytes_total` let a stage report byte-level progress (a download)
alongside its unit-level progress (files); `downloader.py` is the first caller to use
them.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import Protocol, runtime_checkable


def _fmt_bytes(n: int) -> str:
    """Local, tiny, and deliberately not shared with `downloader._fmt_bytes` --
    importing from there would make `downloader` un-importable before `reporter`."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@runtime_checkable
class Reporter(Protocol):
    """What a pipeline stage may tell the outside world about its progress."""

    def stage(
        self,
        name: str,
        total: int | None = None,
        *,
        stage_index: int | None = None,
        stage_count: int | None = None,
    ) -> None:
        """A stage called `name` is starting, with `total` units of work if known.

        `stage_index`/`stage_count` are the stage's 1-based position in the whole
        pipeline run, if the caller knows it (e.g. "3 of 7").
        """

    def progress(
        self,
        done: int,
        total: int | None,
        label: str = "",
        *,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        """`done` of `total` units finished; `label` describes the unit just finished.

        `bytes_done`/`bytes_total` are an optional byte-level view of the same
        progress (e.g. a download's running total), for a caller that can compute it.
        """

    def log(self, msg: str) -> None:
        """A line of ordinary detail."""

    def warn(self, msg: str) -> None:
        """Something the user should see but that does not stop the stage."""

    def done(self, name: str, summary: str = "") -> None:
        """The stage called `name` finished; `summary` is a one-line result."""


class NullReporter:
    """A `Reporter` that discards everything."""

    def stage(
        self,
        name: str,
        total: int | None = None,
        *,
        stage_index: int | None = None,
        stage_count: int | None = None,
    ) -> None:
        return None

    def progress(
        self,
        done: int,
        total: int | None,
        label: str = "",
        *,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
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

    def stage(
        self,
        name: str,
        total: int | None = None,
        *,
        stage_index: int | None = None,
        stage_count: int | None = None,
    ) -> None:
        prefix = f"Stage {stage_index} of {stage_count} - " if stage_index and stage_count else ""
        suffix = f" ({total})" if total is not None else ""
        self._write(f"\n== {prefix}{name}{suffix}")

    def progress(
        self,
        done: int,
        total: int | None,
        label: str = "",
        *,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        counter = f"[{done}/{total}]" if total is not None else f"[{done}]"
        line = f"{counter} {label}".rstrip()
        if bytes_done is not None:
            bytes_part = _fmt_bytes(bytes_done)
            if bytes_total:
                bytes_part += f" of {_fmt_bytes(bytes_total)}"
            line = f"{line}  {bytes_part}"
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


class _LineReporterStream(io.TextIOBase):
    """Redirects `print()`-based output (tools.py has no `Reporter` hooks) to a Reporter."""

    def __init__(self, rep: Reporter):
        self._rep = rep
        self._buf = ""

    def write(self, text: str) -> int:  # type: ignore[override]
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._rep.log(line)
        return len(text)

    def flush(self) -> None:
        return None


@contextlib.contextmanager
def stdout_to_reporter(rep: Reporter):
    """Route `print()` output from a legacy stdout-based command through `rep.log`."""
    with contextlib.redirect_stdout(_LineReporterStream(rep)):
        yield
