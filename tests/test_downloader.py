"""Tests for downloader.py: `_write_meta` (Nexus vs direct) and `mo2_game_name`."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from collections2wabbajack import downloader
from collections2wabbajack.nexus import NexusError


def _read_meta(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    cfg.read(path, encoding="utf-8")
    return cfg


def test_write_meta_nexus_mode_has_no_directurl(tmp_path: Path):
    meta_path = tmp_path / "mymod.7z.meta"
    downloader._write_meta(
        meta_path,
        game_name="SkyrimSE",
        mod_id=123,
        file_id=456,
        name="My Mod File",
        mod_name="My Mod",
        version="1.2.3",
        file_category="1",
    )
    cfg = _read_meta(meta_path)
    general = cfg["General"]
    assert general["gameName"] == "SkyrimSE"
    assert general["modID"] == "123"
    assert general["fileID"] == "456"
    assert general["name"] == "My Mod File"
    assert general["modName"] == "My Mod"
    assert general["version"] == "1.2.3"
    assert general["repository"] == "Nexus"
    assert general["url"] == ""
    assert "directURL" not in general


def test_write_meta_direct_mode_sets_directurl_and_empty_repository(tmp_path: Path):
    meta_path = tmp_path / "myfile.zip.meta"
    url = "https://example.com/files/myfile.zip"
    downloader._write_meta(
        meta_path,
        game_name="SkyrimSE",
        mod_id=0,
        file_id=0,
        name="My Direct Mod",
        mod_name="My Direct Mod",
        version="",
        direct_url=url,
        repository="",
    )
    cfg = _read_meta(meta_path)
    general = cfg["General"]
    assert general["directURL"] == url
    assert general["url"] == url
    assert general["repository"] == ""
    assert general["modID"] == "0"
    assert general["fileID"] == "0"


def test_mo2_game_name_known_domain():
    assert downloader.mo2_game_name("skyrimspecialedition") == "SkyrimSE"
    assert downloader.mo2_game_name("fallout4") == "Fallout4"


def test_mo2_game_name_unknown_domain_raises():
    with pytest.raises(NexusError) as exc_info:
        downloader.mo2_game_name("not-a-real-game-domain")
    assert "not-a-real-game-domain" in str(exc_info.value)


# -- _ByteTracker: the cumulative bytes-done/bytes-total behind the GUI's rate line ----


class _RecordingReporter:
    """Records every `progress()` call's `(done, total, label, bytes_done,
    bytes_total)`; everything else is a no-op."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def stage(self, name, total=None, **_kwargs):
        return None

    def progress(self, done, total, label="", *, bytes_done=None, bytes_total=None):
        self.calls.append((done, total, label, bytes_done, bytes_total))

    def log(self, msg):
        return None

    def warn(self, msg):
        return None

    def done(self, name, summary=""):
        return None


def test_byte_tracker_file_done_always_reports():
    rep = _RecordingReporter()
    tracker = downloader._ByteTracker(rep, total_files=3, total_bytes=3_000_000)
    tracker.file_done("mod-a.7z", extra_bytes=1_000_000)
    tracker.file_done("mod-b.7z", extra_bytes=1_000_000)
    assert len(rep.calls) == 2
    assert rep.calls[0] == (1, 3, "mod-a.7z", 1_000_000, 3_000_000)
    assert rep.calls[1] == (2, 3, "mod-b.7z", 2_000_000, 3_000_000)


def test_byte_tracker_add_bytes_throttles_to_every_2mib():
    rep = _RecordingReporter()
    tracker = downloader._ByteTracker(rep, total_files=1, total_bytes=10 << 20)
    # Five 1 MiB chunks: only the 2nd and 4th cross the 2 MiB threshold.
    for _ in range(5):
        tracker.add_bytes(1 << 20, "big-file.7z")
    assert len(rep.calls) == 2
    assert rep.calls[0][3] == 2 << 20
    assert rep.calls[1][3] == 4 << 20


def test_byte_tracker_zero_total_bytes_reports_none():
    rep = _RecordingReporter()
    tracker = downloader._ByteTracker(rep, total_files=1, total_bytes=0)
    tracker.file_done("unknown-size.7z")
    assert rep.calls[0][4] is None  # bytes_total, not a misleading 0


def test_run_download_sums_manifest_file_sizes_for_bytes_total(tmp_path, monkeypatch):
    """`run_download`'s expected byte total comes from the manifest's
    `source.fileSize` -- no network calls -- and every progress() call carries it."""
    manifest = {
        "info": {"domainName": "skyrimspecialedition"},
        "mods": [
            {
                "name": "Mod A",
                "source": {
                    "type": "nexus",
                    "modId": 1,
                    "fileId": 10,
                    "md5": "a" * 32,
                    "fileSize": 1_000_000,
                },
            },
            {
                "name": "Mod B",
                "source": {
                    "type": "nexus",
                    "modId": 2,
                    "fileId": 20,
                    "md5": "b" * 32,
                    "fileSize": 2_000_000,
                },
            },
        ],
    }
    manifest_path = tmp_path / "collection.json"
    import json

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Every mod resolves as "unsupported" (no real Nexus client wired up) -- fine,
    # this test only cares that bytes_total is derived from the manifest up front.
    monkeypatch.setattr(
        downloader,
        "_download_mod",
        lambda clients, sessions, mod, out_dir, domain, game_name, tracker=None: (
            downloader._unsupported_entry(mod)
        ),
    )

    rep = _RecordingReporter()
    out_dir = tmp_path / "downloads"
    downloader.run_download(
        manifest_path=manifest_path,
        out_dir=out_dir,
        jobs=1,
        limit=None,
        include_optional=True,
        api_key="fake",
        reporter=rep,
    )
    assert rep.calls, "expected at least one progress() call"
    assert all(call[4] == 3_000_000 for call in rep.calls)  # bytes_total on every call
