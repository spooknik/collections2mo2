"""Pure helpers for the progress widgets: a short moving-window rate/ETA estimator
plus text formatters. Deliberately Qt-free so they are trivial to unit test and so
`progress_widget.py` (which does need Qt) stays a thin layer on top of these.
"""

from __future__ import annotations

import time
from collections import deque


class RateEstimator:
    """Bytes/sec over a short trailing window, plus an ETA to a known total.

    Fed by `(bytes_done, timestamp)` samples via `update()`; only samples within
    `window_seconds` of the latest one are kept, so the rate reacts to the last few
    seconds of throughput rather than smearing over an entire multi-hour download.
    Call `reset()` whenever a new stage/download starts so a stale rate from the
    previous one never leaks into the new line.
    """

    def __init__(self, window_seconds: float = 5.0):
        self._window = window_seconds
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def update(self, bytes_done: int, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self._samples.append((t, bytes_done))
        cutoff = t - self._window
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate_bytes_per_sec(self) -> float | None:
        """`None` until at least two samples have landed, or if they are simultaneous
        (can't compute a rate from a zero-width window)."""
        if len(self._samples) < 2:
            return None
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return None
        return max(0.0, (b1 - b0) / dt)

    def eta_seconds(self, bytes_total: int | None) -> float | None:
        """`None` when `bytes_total` is unknown/zero or the rate can't be computed
        yet; `0.0` once `bytes_total` has already been reached."""
        if not bytes_total or len(self._samples) < 2:
            return None
        rate = self.rate_bytes_per_sec()
        if not rate:
            return None
        _, last_done = self._samples[-1]
        remaining = bytes_total - last_done
        if remaining <= 0:
            return 0.0
        return remaining / rate


def format_bytes(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_duration(seconds: float) -> str:
    """`0:14`, `12:03`, `1:02:14` -- no fractional seconds, hours only when needed."""
    total = max(0, round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_rate_line(
    bytes_done: int,
    bytes_total: int | None,
    rate: float | None,
    eta: float | None,
) -> str:
    """`"48.3 MB/s - 1.21 GB of 1.88 GB - ETA 0:14"`, degrading gracefully as pieces
    of that (rate, total, ETA) are unavailable."""
    parts: list[str] = []
    if rate is not None:
        parts.append(f"{format_bytes(rate)}/s")
    if bytes_total:
        parts.append(f"{format_bytes(bytes_done)} of {format_bytes(bytes_total)}")
    elif bytes_done:
        parts.append(format_bytes(bytes_done))
    if eta is not None:
        parts.append(f"ETA {format_duration(eta)}")
    return " - ".join(parts)
