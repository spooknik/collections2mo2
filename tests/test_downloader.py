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
