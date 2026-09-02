"""Unit tests for the rate/ETA estimator and text formatters behind the progress
page's rate line. Deliberately Qt-free (`progress_calc.py` has no Qt import) so these
run fast and without `QT_QPA_PLATFORM=offscreen`.
"""

from __future__ import annotations

from collections2wabbajack.gui.progress_calc import (
    RateEstimator,
    format_bytes,
    format_duration,
    format_rate_line,
)


def test_rate_estimator_needs_two_samples():
    est = RateEstimator()
    assert est.rate_bytes_per_sec() is None
    est.update(1000, now=0.0)
    assert est.rate_bytes_per_sec() is None  # still just one sample


def test_rate_estimator_computes_bytes_per_second():
    est = RateEstimator(window_seconds=5.0)
    est.update(0, now=0.0)
    est.update(1_000_000, now=1.0)
    assert est.rate_bytes_per_sec() == 1_000_000.0


def test_rate_estimator_drops_samples_outside_the_window():
    est = RateEstimator(window_seconds=5.0)
    est.update(0, now=0.0)
    est.update(1_000_000, now=1.0)
    # Jump far ahead: the t=0 sample should be dropped, leaving only two close
    # samples, so the rate reflects the last second's throughput, not the whole run.
    est.update(2_000_000, now=20.0)
    est.update(2_500_000, now=20.5)
    rate = est.rate_bytes_per_sec()
    assert rate is not None
    assert rate == 1_000_000.0  # (2_500_000 - 2_000_000) / 0.5


def test_rate_estimator_eta_seconds():
    est = RateEstimator()
    est.update(0, now=0.0)
    est.update(1_000_000, now=1.0)  # 1 MB/s
    eta = est.eta_seconds(bytes_total=5_000_000)
    assert eta == 4.0  # 4 MB remaining at 1 MB/s


def test_rate_estimator_eta_none_without_total_or_samples():
    est = RateEstimator()
    assert est.eta_seconds(bytes_total=None) is None
    assert est.eta_seconds(bytes_total=0) is None
    est.update(0, now=0.0)
    assert est.eta_seconds(bytes_total=1000) is None  # only one sample yet


def test_rate_estimator_eta_zero_once_total_reached():
    est = RateEstimator()
    est.update(0, now=0.0)
    est.update(1000, now=1.0)
    assert est.eta_seconds(bytes_total=500) == 0.0


def test_rate_estimator_reset_clears_history():
    est = RateEstimator()
    est.update(0, now=0.0)
    est.update(1_000_000, now=1.0)
    assert est.rate_bytes_per_sec() is not None
    est.reset()
    assert est.rate_bytes_per_sec() is None


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1.5 * 1024 * 1024) == "1.50 MB"
    assert format_bytes(1.88 * 1024**3) == "1.88 GB"


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(14) == "0:14"
    assert format_duration(65) == "1:05"
    assert format_duration(3723) == "1:02:03"


def test_format_rate_line_full():
    line = format_rate_line(
        bytes_done=int(1.21 * 1024**3),
        bytes_total=int(1.88 * 1024**3),
        rate=48.3 * 1024 * 1024,
        eta=14,
    )
    assert line == "48.30 MB/s - 1.21 GB of 1.88 GB - ETA 0:14"


def test_format_rate_line_degrades_without_total_or_eta():
    line = format_rate_line(bytes_done=1000, bytes_total=None, rate=None, eta=None)
    assert line == "1000 B"


def test_format_rate_line_empty_when_nothing_known():
    assert format_rate_line(bytes_done=0, bytes_total=None, rate=None, eta=None) == ""
