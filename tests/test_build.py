"""Tests for build._rewrite_ini(): line-based ModOrganizer.ini rewriting."""

from __future__ import annotations

from pathlib import Path

from collections2wabbajack.build import _rewrite_ini


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
