"""Tests for downloader.py: `_write_meta` (Nexus vs direct) and `mo2_game_name`."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from collections2mo2 import downloader
from collections2mo2.nexus import NexusError


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
        lambda clients, sessions, mod, out_dir, domain, game_name, tracker=None, bundle_dir=None: (
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


# -- bundle sources: mods the curator packed into the collection archive ---------------


def _bundle_manifest(tmp_path: Path, expression: str, logical: str = "my patch.7z") -> Path:
    import json

    manifest = {
        "info": {"domainName": "skyrimspecialedition"},
        "mods": [
            {
                "name": logical,
                "version": "",
                "source": {
                    "type": "bundle",
                    "fileSize": 3,
                    "logicalFilename": logical,
                    "updatePolicy": "exact",
                    "tag": "bundletag1",
                    "fileExpression": expression,
                },
                "details": {"category": "", "type": ""},
                "phase": 0,
            }
        ],
    }
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / "collection.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _bundled_tree(manifest_path: Path, expression: str) -> Path:
    root = manifest_path.parent / "bundled" / expression
    for rel, data in (
        ("textures/actors/character/khajiitfemale/head.dds", b"a"),
        ("textures/actors/character/khajiitfemale/head_msn.dds", b"bb"),
        ("textures/actors/character/KhajiitMale/head_msn.dds", b"ccc"),
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _bundle_mod(manifest_path: Path):
    import json

    return json.loads(manifest_path.read_text(encoding="utf-8"))["mods"][0]


def test_download_bundle_folder_is_zipped_without_a_wrapper_folder(tmp_path: Path):
    import zipfile

    expression = "Bundled - my patch.7z (v)"
    manifest_path = _bundle_manifest(tmp_path, expression)
    _bundled_tree(manifest_path, expression)
    out_dir = tmp_path / "downloads"

    entry = downloader._download_bundle(
        _bundle_mod(manifest_path),
        out_dir,
        "SkyrimSE",
        manifest_path.parent / "bundled",
    )

    assert entry.status == "ok"
    assert entry.source_type == "bundle"
    assert entry.file_name == "Bundled - my patch.zip"
    assert entry.mod_id is None and entry.file_id is None and entry.url is None
    dest = out_dir / "Bundled - my patch.zip"
    assert Path(entry.path) == dest.resolve()
    assert entry.size == dest.stat().st_size
    assert entry.md5 == downloader._md5_of_file(dest)

    with zipfile.ZipFile(dest) as zf:
        names = sorted(zf.namelist())
    assert names == [
        "textures/actors/character/KhajiitMale/head_msn.dds",
        "textures/actors/character/khajiitfemale/head.dds",
        "textures/actors/character/khajiitfemale/head_msn.dds",
    ]

    meta = _read_meta(dest.with_name(dest.name + ".meta"))["General"]
    assert meta["repository"] == ""
    assert meta["modID"] == "0" and meta["fileID"] == "0"
    assert "directURL" not in meta


def test_download_bundle_falls_back_to_logical_filename_then_stem(tmp_path: Path):
    # The folder is named neither after fileExpression nor logicalFilename, but
    # contains the logical name's stem -- the case-insensitive last resort.
    manifest_path = _bundle_manifest(tmp_path, "Bundled - not-this-name.7z (v)")
    _bundled_tree(manifest_path, "MY PATCH (bundled)")

    entry = downloader._download_bundle(
        _bundle_mod(manifest_path),
        tmp_path / "downloads",
        "SkyrimSE",
        manifest_path.parent / "bundled",
    )
    assert entry.status == "ok"
    assert entry.file_name == "Bundled - my patch.zip"


def test_download_bundle_file_entry_is_copied_under_its_own_name(tmp_path: Path):
    expression = "Bundled - my patch.7z (v)"
    manifest_path = _bundle_manifest(tmp_path, expression)
    bundled = manifest_path.parent / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / expression).write_bytes(b"7z-ish payload")

    out_dir = tmp_path / "downloads"
    entry = downloader._download_bundle(_bundle_mod(manifest_path), out_dir, "SkyrimSE", bundled)

    assert entry.status == "ok"
    assert entry.file_name == expression
    dest = out_dir / expression
    assert dest.read_bytes() == b"7z-ish payload"
    assert entry.size == len(b"7z-ish payload")
    assert dest.with_name(dest.name + ".meta").exists()


def test_download_bundle_missing_content_is_unsupported_with_a_clear_error(tmp_path: Path):
    manifest_path = _bundle_manifest(tmp_path, "Bundled - gone.7z (v)")
    (manifest_path.parent / "bundled").mkdir(parents=True, exist_ok=True)

    entry = downloader._download_bundle(
        _bundle_mod(manifest_path),
        tmp_path / "downloads",
        "SkyrimSE",
        manifest_path.parent / "bundled",
    )
    assert entry.status == "unsupported"
    assert entry.error == "bundled file not found in collection archive: Bundled - gone.7z (v)"
    assert entry.path is None


def test_download_bundle_rerun_keeps_the_existing_zip(tmp_path: Path):
    expression = "Bundled - my patch.7z (v)"
    manifest_path = _bundle_manifest(tmp_path, expression)
    _bundled_tree(manifest_path, expression)
    out_dir = tmp_path / "downloads"
    mod = _bundle_mod(manifest_path)

    first = downloader._download_bundle(mod, out_dir, "SkyrimSE", manifest_path.parent / "bundled")
    dest = Path(first.path)
    stamp = dest.stat().st_mtime_ns
    payload = dest.read_bytes()

    second = downloader._download_bundle(mod, out_dir, "SkyrimSE", manifest_path.parent / "bundled")
    assert second.status == "skipped"
    assert second.md5 == first.md5
    assert dest.stat().st_mtime_ns == stamp
    assert dest.read_bytes() == payload


def test_run_download_packs_bundle_mods_without_a_network_client(tmp_path: Path):
    import json

    expression = "Bundled - my patch.7z (v)"
    manifest_path = _bundle_manifest(tmp_path, expression)
    _bundled_tree(manifest_path, expression)
    out_dir = tmp_path / "downloads"
    json_path = tmp_path / "downloads.json"

    rc = downloader.run_download(
        manifest_path=manifest_path,
        out_dir=out_dir,
        jobs=1,
        limit=None,
        include_optional=True,
        api_key=None,
        json_path=json_path,
        reporter=_RecordingReporter(),
    )
    assert rc == 0
    entries = json.loads(json_path.read_text(encoding="utf-8"))["entries"]
    assert [e["status"] for e in entries] == ["ok"]
    assert entries[0]["source_type"] == "bundle"
    assert Path(entries[0]["path"]).exists()
