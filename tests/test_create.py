"""Tests for `create._finish`'s summary output."""

from __future__ import annotations

from pathlib import Path

from collections2mo2 import create


class _CollectingReporter:
    def __init__(self):
        self.logs: list[str] = []
        self.warnings: list[str] = []

    def stage(self, name, total=None, *, stage_index=None, stage_count=None) -> None:
        return None

    def progress(self, done, total, label="", *, bytes_done=None, bytes_total=None) -> None:
        return None

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def done(self, name: str, summary: str = "") -> None:
        return None


def test_finish_prints_first_start_hint_with_mod_count(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    run.record("install", "ok", "42 mods installed")
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0, mod_count=42)
    assert rc == 0
    assert any("first start indexes 42 mods" in line for line in rep.logs)


def test_finish_omits_hint_when_mod_count_not_given(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0)
    assert rc == 0
    assert not any("first start indexes" in line for line in rep.logs)


def test_finish_omits_hint_when_run_failed(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    run.record("install", "failed", "boom")
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0, mod_count=42)
    assert rc == 1
    assert not any("first start indexes" in line for line in rep.logs)
