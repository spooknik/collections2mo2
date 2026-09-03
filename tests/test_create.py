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


def test_install_tools_stage_runs_after_ledger_and_records_ok(monkeypatch, tmp_path: Path):
    from collections2mo2 import tools

    calls = []

    def fake_install(ns):
        calls.append(ns)
        print("  downloading xEdit")  # tools.py prints; must reach the reporter
        return 0

    monkeypatch.setattr(tools, "cmd_tools_install", fake_install)
    rep = _CollectingReporter()
    run = create.Run(rep)
    paths = create.Paths(tmp_path / "inst")

    rc = create.install_tools_stage(paths, ["xedit", "loot"], run, rep)

    assert rc == 0
    assert len(calls) == 1
    assert calls[0].ids == ["xedit", "loot"]
    assert Path(calls[0].mo2_dir) == paths.out
    assert calls[0].all_default is False and calls[0].force is False
    assert [(s.name, s.status) for s in run.stages] == [("tools", "ok")]
    assert not run.failed
    assert "  downloading xEdit" in rep.logs


def test_install_tools_stage_failure_is_a_failed_stage(monkeypatch, tmp_path: Path):
    from collections2mo2 import tools

    monkeypatch.setattr(tools, "cmd_tools_install", lambda ns: 1)
    rep = _CollectingReporter()
    run = create.Run(rep)

    rc = create.install_tools_stage(create.Paths(tmp_path / "inst"), ["xedit"], run, rep)

    assert rc == 1
    assert run.failed
    assert run.stages[0].name == "tools"
    assert any("tools failed" in w for w in rep.warnings)


# -- report_game_version: advisory, never a failed stage ---------------------------------


def test_report_game_version_logs_a_match(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.1170.0"
    )
    rep = _CollectingReporter()
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}

    result = create.report_game_version(manifest, "D:/Skyrim", "SkyrimSE", rep)

    assert result == ("match", "Game version 1.6.1170 matches the collection.")
    assert rep.logs == ["Game version 1.6.1170 matches the collection."]
    assert rep.warnings == []


def test_report_game_version_warns_on_a_mismatch(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.640.0"
    )
    rep = _CollectingReporter()
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}

    status, _message = create.report_game_version(manifest, "D:/Skyrim", "SkyrimSE", rep)

    assert status == "mismatch"
    assert rep.logs == []
    assert rep.warnings == [message]
    assert "Skyrim Special Edition 1.6.1170" in message
    assert "the game at D:/Skyrim is 1.6.640" in message


def test_report_game_version_warns_when_the_version_cannot_be_read(monkeypatch):
    monkeypatch.setattr(create.game_version, "installed_game_version", lambda path, name: None)
    rep = _CollectingReporter()

    status, _message = create.report_game_version(
        {"info": {"gameVersions": ["1.6.1170.0"]}}, "D:/Skyrim", "SkyrimSE", rep
    )

    assert status == "unknown"
    assert rep.warnings == [message]


def test_report_game_version_is_silent_without_a_manifest_version(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.1170.0"
    )
    rep = _CollectingReporter()

    assert create.report_game_version({"info": {}}, "D:/Skyrim", "SkyrimSE", rep) is None
    assert rep.logs == [] and rep.warnings == []


def test_report_game_version_addon_mismatch_is_a_note_not_a_warning(monkeypatch):
    monkeypatch.setattr(create.game_version, "installed_game_version", lambda *a, **k: "1.7.104.0")
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}
    rep = _CollectingReporter()
    status, _message = create.report_game_version(
        manifest, "D:/Skyrim", "SkyrimSE", rep, is_base=False
    )
    assert status == "mismatch"
    assert rep.warnings == []
    assert any("add-on collection" in line for line in rep.logs)
