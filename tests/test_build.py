"""Tests for build._rewrite_ini(): line-based ModOrganizer.ini rewriting, plus keeping
the MO2/Root Builder release archives in downloads/ so Wabbajack can reference them.
"""

from __future__ import annotations

import configparser
import zipfile
from pathlib import Path

import pytest

from collections2mo2.build import (
    _archive_top_level_names,
    _ensure_release_download,
    _rewrite_ini,
)
from collections2mo2.sevenzip import TOOLS_DIR

_SEVENZIP_AVAILABLE = (TOOLS_DIR / "7za.exe").exists() and (TOOLS_DIR / "7z.dll").exists()


def _base_ini(game_edition: str | None = None) -> str:
    lines = [
        "[General]",
        "gameName=Skyrim Special Edition",
        "gamePath=",
    ]
    if game_edition is not None:
        lines.append(f"game_edition={game_edition}")
    lines += [
        "selected_profile=@ByteArray(Default)",
        "",
        "[customExecutables]",
        "size=2",
        "1\\title=SKSE",
        "1\\binary=SKSE/skse64_loader.exe",
        "1\\arguments=",
        "1\\workingDirectory=",
        "2\\title=Old Tool",
        "2\\binary=C:/OldGame/SKSE/skse64_loader.exe",
        "2\\arguments=",
        "2\\workingDirectory=Z:/DoesNotExist/Some/Path",
    ]
    return "\n".join(lines) + "\n"


def test_rewrite_ini_relative_binary_is_prefixed_with_game_path(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    changes = _rewrite_ini(ini_path, game_path="D:/Games/Skyrim")

    text = ini_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "1\\binary=D:/Games/Skyrim/SKSE/skse64_loader.exe" in lines
    assert any("1\\binary" in c for c in changes)


def test_rewrite_ini_absolute_binary_under_source_is_rebased_to_stock(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    assert "2\\binary=D:/StockGame/SKSE/skse64_loader.exe" in lines


def test_rewrite_ini_nonexistent_working_directory_replaced_with_game_path(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    # Z:/DoesNotExist/Some/Path does not exist on this machine and is not under
    # the source game path, so it falls back to game_path.
    assert "2\\workingDirectory=D:/StockGame" in lines


def test_rewrite_ini_existing_game_edition_is_preserved(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(game_edition="GOG"), encoding="utf-8", newline="\n")

    _rewrite_ini(ini_path, game_path="D:/Games/Skyrim")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    assert "game_edition=GOG" in lines
    assert "game_edition=Steam" not in lines


def test_rewrite_ini_adds_missing_gamepath_and_edition(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    # No game_edition line at all, and gamePath is empty.
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    changes = _rewrite_ini(ini_path, game_path="D:/Games/Skyrim")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    assert "game_edition=Steam" in lines
    assert any("game_edition" in c for c in changes)


def test_rewrite_ini_is_idempotent_on_second_run(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")
    text_after_first = ini_path.read_text(encoding="utf-8")

    changes_second = _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")
    text_after_second = ini_path.read_text(encoding="utf-8")

    assert text_after_first == text_after_second
    assert changes_second == []


# -- rebasing xEdit/DynDOLOD/TexGen-style -D: arguments -------------------------------

# The escaped form of a logical `-D:"C:\OldGame\Data"` argument, exactly as MO2/Qt
# writes it to disk (verified against a working Wabbajack Stock Game instance,
# D:\Lorerim\ModOrganizer.ini: backslashes doubled, quotes escaped with `\`).
_XEDIT_ARGS_INI = '-D:\\"C:\\\\OldGame\\\\Data\\" -sse'


def _xedit_ini() -> str:
    return (
        "\n".join(
            [
                "[General]",
                "gameName=Skyrim Special Edition",
                "gamePath=",
                "selected_profile=@ByteArray(Default)",
                "",
                "[customExecutables]",
                "size=1",
                "1\\title=xEdit (SSE)",
                "1\\binary=C:/OldGame/tools/xedit/xTESEdit64.exe",
                f"1\\arguments={_XEDIT_ARGS_INI}",
                "1\\workingDirectory=",
            ]
        )
        + "\n"
    )


def test_rewrite_ini_rebases_quoted_data_path_in_arguments(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_xedit_ini(), encoding="utf-8", newline="\n")

    changes = _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    new_args = next(l.split("=", 1)[1] for l in lines if l.startswith("1\\arguments="))
    assert new_args == '-D:\\"D:\\\\StockGame\\\\Data\\" -sse'
    assert any("1\\arguments" in c for c in changes)


def test_rewrite_ini_leaves_arguments_without_source_path_untouched(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_base_ini(), encoding="utf-8", newline="\n")

    changes = _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")

    lines = ini_path.read_text(encoding="utf-8").splitlines()
    assert "1\\arguments=" in lines  # SKSE's empty arguments= is untouched
    assert not any("1\\arguments" in c for c in changes)


def test_rewrite_ini_arguments_rebase_is_idempotent(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_text(_xedit_ini(), encoding="utf-8", newline="\n")

    _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")
    text_after_first = ini_path.read_text(encoding="utf-8")

    changes_second = _rewrite_ini(ini_path, game_path="D:/StockGame", source_game_path="C:/OldGame")
    assert ini_path.read_text(encoding="utf-8") == text_after_first
    assert changes_second == []


# -- keeping the release archives as real downloads/ ---------------------------------


def test_ensure_release_download_copies_archive_and_writes_direct_meta(tmp_path: Path):
    mo2_dir = tmp_path / "inst"
    mo2_dir.mkdir()
    archive = tmp_path / "Mod.Organizer-2.5.2.7z"
    archive.write_bytes(b"fake mo2 archive")

    dest = _ensure_release_download(
        mo2_dir,
        archive,
        "https://github.com/ModOrganizer2/modorganizer/releases/download/v2.5.2/Mod.Organizer-2.5.2.7z",
        "Mod Organizer 2",
        "2.5.2",
    )

    assert dest == mo2_dir / "downloads" / "Mod.Organizer-2.5.2.7z"
    assert dest.read_bytes() == b"fake mo2 archive"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(dest.with_name(dest.name + ".meta"), encoding="utf-8")
    general = cfg["General"]
    assert general["directURL"] == (
        "https://github.com/ModOrganizer2/modorganizer/releases/download/v2.5.2/Mod.Organizer-2.5.2.7z"
    )
    assert general["modName"] == "Mod Organizer 2"
    assert general["version"] == "2.5.2"
    assert general["repository"] == ""
    assert general["installed"] == "false"


def test_ensure_release_download_is_idempotent(tmp_path: Path):
    mo2_dir = tmp_path / "inst"
    mo2_dir.mkdir()
    archive = tmp_path / "rootbuilder.5.1.1.zip"
    archive.write_bytes(b"v1")

    _ensure_release_download(
        mo2_dir, archive, "https://example.test/rb.zip", "Root Builder", "5.1.1"
    )
    dest = mo2_dir / "downloads" / "rootbuilder.5.1.1.zip"
    # A cache re-download that changed on disk must not silently overwrite what is
    # already in downloads/ -- same convention as `_download_cached`.
    archive.write_bytes(b"v2-should-not-be-copied")
    _ensure_release_download(
        mo2_dir, archive, "https://example.test/rb.zip", "Root Builder", "5.1.1"
    )
    assert dest.read_bytes() == b"v1"


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_archive_top_level_names_no_wrapping_folder(tmp_path: Path):
    archive = tmp_path / "mo2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ModOrganizer.exe", "x")
        zf.writestr("dlls/usvfs.dll", "x")
        zf.writestr("plugins/installer_bain.py", "x")
        zf.writestr("stylesheets/1809 Dark.qss", "x")

    assert _archive_top_level_names(archive) == [
        "ModOrganizer.exe",
        "dlls",
        "plugins",
        "stylesheets",
    ]


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_archive_top_level_names_descends_a_single_wrapping_folder(tmp_path: Path):
    archive = tmp_path / "rootbuilder.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("rootbuilder/__init__.py", "x")
        zf.writestr("rootbuilder/base/base.py", "x")
        zf.writestr("rootbuilder/common/common.py", "x")

    assert _archive_top_level_names(archive) == ["__init__.py", "base", "common"]
