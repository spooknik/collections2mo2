"""Tests for `game_version.py`: version comparison and the executable version probe.

The comparison half is pure and runs anywhere. The probe half reads a Windows version
resource, so those tests are either "returns None" (true on every platform) or guarded
on Windows plus a system executable that is actually present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from collections2mo2 import game_version as gv

# -- compare_versions -----------------------------------------------------------------


def test_compare_versions_match():
    result = gv.compare_versions(["1.6.1170.0"], "1.6.1170.0", game_name="SkyrimSE")
    assert result is not None
    status, message = result
    assert status == "match"
    assert message == "Game version 1.6.1170 matches the collection."


def test_compare_versions_matches_on_three_components():
    # Nexus records "1.6.1170.0"; the exe reports "1.6.1170" on some builds, and the
    # fourth component is a build counter neither side agrees on.
    status, _ = gv.compare_versions(["1.6.1170.0"], "1.6.1170")
    assert status == "match"
    status, _ = gv.compare_versions(["1.6.1170"], "1.6.1170.353")
    assert status == "match"


def test_compare_versions_mismatch_mentions_game_path_and_both_versions():
    result = gv.compare_versions(
        ["1.6.1170.0"],
        "1.6.640.0",
        game_name="skyrimspecialedition",
        game_path="D:/Games/Skyrim",
    )
    assert result is not None
    status, message = result
    assert status == "mismatch"
    assert message.startswith("This collection targets Skyrim Special Edition 1.6.1170;")
    assert "the game at D:/Games/Skyrim is 1.6.640" in message
    assert "FAQ on game versions" in message


def test_compare_versions_accepts_any_listed_version():
    status, _ = gv.compare_versions(["1.5.97.0", "1.6.1170.0"], "1.6.1170.0")
    assert status == "match"


def test_compare_versions_unknown_when_installed_is_none():
    result = gv.compare_versions(["1.6.1170.0"], None, game_name="SkyrimSE")
    assert result is not None
    status, message = result
    assert status == "unknown"
    assert "Could not read" in message
    assert "1.6.1170" in message


def test_compare_versions_returns_none_without_collection_versions():
    assert gv.compare_versions([], "1.6.1170.0") is None
    assert gv.compare_versions([""], "1.6.1170.0") is None


def test_short_version_and_display_name():
    assert gv.short_version("1.6.1170.0") == "1.6.1170"
    assert gv.short_version("1.6") == "1.6"
    assert gv.short_version("not a version") == "not a version"
    assert gv.display_game_name("skyrimspecialedition") == "Skyrim Special Edition"
    assert gv.display_game_name("SkyrimSE") == "Skyrim Special Edition"
    assert gv.display_game_name("Skyrim Special Edition") == "Skyrim Special Edition"


def test_manifest_game_versions_reads_info():
    assert gv.manifest_game_versions({"info": {"gameVersions": ["1.6.1170.0"]}}) == ["1.6.1170.0"]
    assert gv.manifest_game_versions({"info": {}}) == []
    assert gv.manifest_game_versions(None) == []


# -- installed_game_version -------------------------------------------------------------


def test_installed_game_version_none_for_missing_exe(tmp_path: Path):
    assert gv.installed_game_version(tmp_path, "Skyrim Special Edition") is None


def test_installed_game_version_none_for_unknown_game(tmp_path: Path):
    assert gv.installed_game_version(tmp_path, "Some Other Game") is None
    assert gv.installed_game_version(None, "Skyrim Special Edition") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version resource API")
def test_file_version_reads_a_real_system_executable():
    notepad = Path("C:/Windows/System32/notepad.exe")
    if not notepad.is_file():
        pytest.skip("notepad.exe is not present")
    version = gv.file_version(notepad)
    assert version is not None
    parts = version.split(".")
    assert len(parts) == 4
    assert all(p.isdigit() for p in parts)


def test_check_game_version_reports_unknown_when_the_exe_is_missing(tmp_path: Path):
    result = gv.check_game_version(["1.6.1170.0"], tmp_path, "SkyrimSE")
    assert result is not None
    assert result[0] == "unknown"
