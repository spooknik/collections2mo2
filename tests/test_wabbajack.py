"""Tests for the Wabbajack export: settings generation and the pre-compile checklist.

The synthetic instance below is the smallest thing that exercises every classification
branch: one Nexus archive, one direct-URL archive, one archive with a useless `.meta`,
one with no `.meta` at all; a collection mod, a user mod, a separator and a tool folder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collections2wabbajack import ledger as ledger_mod
from collections2wabbajack import wabbajack


def _meta(**general: str) -> str:
    body = "\n".join(f"{k}={v}" for k, v in general.items())
    return f"[General]\n{body}\n"


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    root = tmp_path / "inst"
    for sub in ("downloads", "mods", "profiles", "Tools", "c2wj", "overwrite", "Stock Game"):
        (root / sub).mkdir(parents=True)
    (root / "Stock Game" / "SkyrimSE.exe").write_bytes(b"x" * 16)
    # What the collection's Runtime Swapper leaves behind in the game folder.
    swapper = root / "Stock Game" / ".skyrim-runtime-swapper" / "backups" / ".complete"
    swapper.mkdir(parents=True)
    (swapper / "1.6.1170-9.complete").write_bytes(b"c" * 907)
    (root / "profiles" / "Base List").mkdir()
    (root / "profiles" / "Extra").mkdir()
    (root / "ModOrganizer.log").write_text("log", encoding="utf-8")
    # MO2's own program files: no archive in downloads/ can produce these.
    (root / "ModOrganizer.exe").write_bytes(b"m" * 400)
    (root / "dlls").mkdir()
    (root / "dlls" / "usvfs.dll").write_bytes(b"u" * 600)

    dl = root / "downloads"
    (dl / "good.7z").write_bytes(b"a" * 10)
    (dl / "good.7z.meta").write_text(
        _meta(gameName="SkyrimSE", modID="123", fileID="456", repository="Nexus"), encoding="utf-8"
    )
    (dl / "direct.zip").write_bytes(b"b" * 10)
    (dl / "direct.zip.meta").write_text(
        _meta(gameName="SkyrimSE", modID="0", fileID="0", directURL="https://example.test/f.zip"),
        encoding="utf-8",
    )
    (dl / "empty-meta.zip").write_bytes(b"c" * 10)
    (dl / "empty-meta.zip.meta").write_text(
        _meta(gameName="SkyrimSE", modID="0", fileID="0", url=""), encoding="utf-8"
    )
    (dl / "orphan.rar").write_bytes(b"d" * 10)

    mods = root / "mods"
    (mods / "Collection Mod").mkdir()
    (mods / "Collection Mod" / "a.esp").write_bytes(b"e" * 100)
    (mods / "My Test Mod").mkdir()
    (mods / "My Test Mod" / "b.esp").write_bytes(b"f" * 200)
    (mods / "Phase 0_separator").mkdir()
    (root / "Tools" / "xedit").mkdir()
    (root / "Tools" / "xedit" / "xEdit.exe").write_bytes(b"g" * 300)
    return root


@pytest.fixture
def led(instance: Path) -> ledger_mod.Ledger:
    led = ledger_mod.Ledger(instance)
    led.set_game(
        domain="skyrimspecialedition",
        mo2_name="SkyrimSE",
        source_path="E:\\Games\\Skyrim Special Edition",
        stock_game_dir=str(instance / "Stock Game"),
    )
    led.register_layer(
        "h2uqa3",
        68,
        name="SKSE and Behaviours Essentials",
        author="Ja1zinZamp",
        profile="Base List",
        separators=["Phase 0_separator"],
    )
    led.set_mod_owner(
        "Collection Mod", ledger_mod.collection_owner("h2uqa3", 68), md5="deadbeef", tag="t"
    )
    led.save()
    return led


# -- settings ------------------------------------------------------------------------


def test_settings_defaults_come_from_the_ledger(instance: Path, led: ledger_mod.Ledger) -> None:
    s = wabbajack.build_settings(instance, led)
    assert s["ModListName"] == "SKSE and Behaviours Essentials"
    assert s["ModListAuthor"] == "Ja1zinZamp"
    assert s["Game"] == "SkyrimSpecialEdition"
    assert s["ModlistVersion"] == "68.0.0"
    assert s["Profile"] == "Base List"
    assert s["AdditionalProfiles"] == ["Extra"]
    url = "https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3"
    assert s["ModListWebsite"] == url
    assert s["ModListReadme"] == url
    assert "collections2wabbajack" in s["ModListDescription"]
    assert s["Source"] == str(instance).replace("/", "\\")
    assert s["Downloads"] == str(instance / "downloads").replace("/", "\\")
    assert s["OutputFile"].endswith("\\wabbajack\\SKSE and Behaviours Essentials.wabbajack")


def test_settings_use_wabbajacks_exact_key_spelling(instance: Path, led: ledger_mod.Ledger) -> None:
    # A misspelled key is silently dropped by System.Text.Json, so the odd mixture of
    # `ModList*` and `Modlist*` is load-bearing.
    s = wabbajack.build_settings(instance, led)
    for key in ("ModListName", "ModListAuthor", "ModListDescription", "ModListImage"):
        assert key in s
    for key in ("ModlistIsNSFW", "ModlistVersion"):
        assert key in s
    assert "ModListVersion" not in s
    assert "ModListIsNSFW" not in s
    assert s["UseGamePaths"] is True  # what makes the Stock Game copy compile


def test_settings_ignore_covers_our_bookkeeping_and_expands_logs(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    s = wabbajack.build_settings(instance, led)
    assert "c2wj" in s["Ignore"]
    assert "overwrite" in s["Ignore"]
    # Our own output folder lives inside the compile source; if it were not ignored the
    # compiler would try to resolve compile.log and the previous .wabbajack.
    assert "wabbajack" in s["Ignore"]
    # `Ignore` matches path prefixes, not globs, so `*.log` has to be expanded.
    assert "ModOrganizer.log" in s["Ignore"]
    assert not any("*" in entry for entry in s["Ignore"])


def test_settings_overrides_win(instance: Path, led: ledger_mod.Ledger) -> None:
    s = wabbajack.build_settings(
        instance,
        led,
        name="Custom",
        author="me",
        description="d",
        version="1.2.3",
        website="https://w.test",
        readme="https://r.test",
        image="C:/img.png",
        output="D:/out/Custom.wabbajack",
        no_match_include=["Tools", "mods\\My Test Mod"],
    )
    assert s["ModListName"] == "Custom"
    assert s["ModListAuthor"] == "me"
    assert s["ModlistVersion"] == "1.2.3"
    assert s["ModListImage"] == "C:\\img.png"
    assert s["OutputFile"] == "D:\\out\\Custom.wabbajack"
    assert s["NoMatchInclude"] == ["Tools", "mods\\My Test Mod"]


def test_settings_file_lands_in_the_source_root(instance: Path, led: ledger_mod.Ledger) -> None:
    s = wabbajack.build_settings(instance, led)
    path = wabbajack.write_settings(instance, s)
    assert path == instance / "SKSE and Behaviours Essentials.compiler_settings"
    assert json.loads(path.read_text(encoding="utf-8"))["ModListName"] == s["ModListName"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(68, "68.0.0"), ("1.2.3.4", "1.2.3.4"), ("2.0", "2.0.0"), ("1.0.0-rc1", "1.0.0.1"), ("", "0.0.1")],
)
def test_dotnet_version(value: object, expected: str) -> None:
    assert wabbajack.dotnet_version(value) == expected


def test_game_name_falls_back_to_the_mo2_name() -> None:
    assert wabbajack.game_name("skyrimspecialedition", None) == "SkyrimSpecialEdition"
    assert wabbajack.game_name(None, "SkyrimSE") == "SkyrimSpecialEdition"
    assert wabbajack.game_name("fallout4", None) == "Fallout4"
    with pytest.raises(wabbajack.WabbajackError):
        wabbajack.game_name(None, None)


def test_safe_name_strips_path_characters() -> None:
    assert wabbajack.safe_name('A/B:C*D?') == "ABCD"
    assert wabbajack.safe_name("   ") == "Modlist"


# -- checklist -----------------------------------------------------------------------


def test_downloads_are_classified_by_meta_source(instance: Path) -> None:
    checks = {c.name: c for c in wabbajack.check_downloads(instance / "downloads")}
    assert checks["good.7z"].source == "nexus"
    assert checks["direct.zip"].source == "direct"
    assert checks["empty-meta.zip"].source == "unrecognised"
    assert checks["orphan.rar"].source == "missing"
    assert sorted(c.name for c in checks.values() if not c.ok) == ["empty-meta.zip", "orphan.rar"]
    assert "good.7z.meta" not in checks  # the sidecars are not archives


def test_inlined_folders_are_the_ones_with_no_archive(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    inlined = {e.path: e for e in wabbajack.check_inlined(instance, led)}
    assert "mods\\My Test Mod" in inlined
    assert "Tools\\xedit" in inlined
    # A collection mod came out of a download and a separator is instance furniture.
    assert "mods\\Collection Mod" not in inlined
    assert "mods\\Phase 0_separator" not in inlined
    assert inlined["mods\\My Test Mod"].size == 200
    assert inlined["mods\\My Test Mod"].reason == "user mod"
    # MO2's binaries are inlined too, but the instance's own data folders are not.
    assert inlined["ModOrganizer.exe"].reason == "MO2 program file"
    assert inlined["dlls"].size == 600
    for skipped in ("mods", "downloads", "profiles", "Tools", "overwrite", "c2wj", "Stock Game"):
        assert skipped not in inlined
    # The settings file is written into the source root; it must not list itself.
    assert not any(p.endswith(".compiler_settings") for p in inlined)


def test_no_match_include_folds_tool_folders_under_the_tools_prefix(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    entries = wabbajack.no_match_include(wabbajack.precompile_checklist(instance, led))
    assert entries[0] == "Tools"
    assert "Tools\\xedit" not in entries
    assert "mods\\My Test Mod" in entries
    assert "ModOrganizer.exe" in entries


def test_game_folder_tool_state_is_covered_but_not_counted_as_inlined(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    checklist = wabbajack.precompile_checklist(instance, led)
    state = {e.path: e for e in checklist.game_state}
    assert list(state) == ["Stock Game\\.skyrim-runtime-swapper"]
    assert state["Stock Game\\.skyrim-runtime-swapper"].size == 907
    # A single unmatched file inside the game copy is fatal, so it must be listed...
    assert "Stock Game\\.skyrim-runtime-swapper" in wabbajack.no_match_include(checklist)
    # ...but its bulk is game-file backups that DirectMatch resolves, so counting it
    # towards the inlined budget would be misleading.
    assert all(e.path != "Stock Game\\.skyrim-runtime-swapper" for e in checklist.inlined)
    # The game files themselves are not swept up.
    assert not any("SkyrimSE.exe" in p for p in state)


def test_checklist_warns_about_unusable_metas_and_reports_stock_game(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    checklist = wabbajack.precompile_checklist(instance, led)
    assert checklist.stock_game == instance / "Stock Game"
    by_path = {e.path: e.size for e in checklist.inlined}
    assert by_path["mods\\My Test Mod"] + by_path["Tools\\xedit"] == 500
    assert checklist.inlined_bytes == sum(by_path.values())
    assert any("no usable .meta" in w for w in checklist.warnings)
    assert any("Mod Organizer's own program files" in w for w in checklist.warnings)
    assert not any("Stock Game folder" in w for w in checklist.warnings)


def test_checklist_warns_when_the_stock_game_is_missing(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    import shutil

    led.set_game(stock_game_dir=None)
    shutil.rmtree(instance / "Stock Game")
    checklist = wabbajack.precompile_checklist(instance, led)
    assert checklist.stock_game is None
    assert checklist.game_state == []
    assert any("no Stock Game folder" in w for w in checklist.warnings)


def test_checklist_warns_over_the_inline_budget(
    instance: Path, led: ledger_mod.Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wabbajack, "INLINE_WARN_BYTES", 100)
    checklist = wabbajack.precompile_checklist(instance, led)
    assert any("stored inside the .wabbajack" in w for w in checklist.warnings)


@pytest.mark.parametrize(
    ("general", "expected"),
    [
        ({"gameName": "SkyrimSE", "modID": "1", "fileID": "2"}, "nexus"),
        ({"gameName": "SkyrimSE", "modID": "0", "fileID": "0"}, "unrecognised"),
        ({"directURL": "https://x.test/a.zip"}, "direct"),
        ({"url": "https://x.test/a.zip"}, "direct"),
        ({"url": "not a url"}, "unrecognised"),
        ({}, "unrecognised"),
    ],
)
def test_classify_meta(general: dict[str, str], expected: str) -> None:
    assert wabbajack.classify_meta(general)[0] == expected


# -- command -------------------------------------------------------------------------


def test_dry_run_writes_only_the_settings_file_and_the_output_dir(
    instance: Path, led: ledger_mod.Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wabbajack, "find_wabbajack_cli", lambda explicit=None: None)
    before = (instance / ledger_mod.LEDGER_NAME).read_text(encoding="utf-8")
    args = _args(instance, dry_run=True)
    assert wabbajack.cmd_wabbajack(args, reporter=_Nullish()) == 0
    settings = instance / "SKSE and Behaviours Essentials.compiler_settings"
    assert settings.exists()
    assert (instance / "wabbajack").is_dir()
    # A dry run must not touch the ledger.
    assert (instance / ledger_mod.LEDGER_NAME).read_text(encoding="utf-8") == before
    # No compile happened, so no modlist file.
    assert not list((instance / "wabbajack").glob("*.wabbajack"))


def test_dry_run_generates_a_placeholder_image(
    instance: Path, led: ledger_mod.Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wabbajack, "find_wabbajack_cli", lambda explicit=None: None)
    wabbajack.cmd_wabbajack(_args(instance, dry_run=True), reporter=_Nullish())
    settings = json.loads(
        (instance / "SKSE and Behaviours Essentials.compiler_settings").read_text(encoding="utf-8")
    )
    image = Path(settings["ModListImage"].replace("\\", "/"))
    assert image.exists()
    assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_user_mods_are_added_to_nomatchinclude(
    instance: Path, led: ledger_mod.Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wabbajack, "find_wabbajack_cli", lambda explicit=None: None)
    wabbajack.cmd_wabbajack(_args(instance, dry_run=True), reporter=_Nullish())
    settings = json.loads(
        (instance / "SKSE and Behaviours Essentials.compiler_settings").read_text(encoding="utf-8")
    )
    assert "Tools" in settings["NoMatchInclude"]
    assert "mods\\My Test Mod" in settings["NoMatchInclude"]
    # Tool folders are covered by the `Tools` prefix, not listed one by one.
    assert "Tools\\xedit" not in settings["NoMatchInclude"]


def test_missing_ledger_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert wabbajack.cmd_wabbajack(_args(tmp_path / "empty", dry_run=True), reporter=_Nullish()) == 1


def test_record_in_ledger_merges_without_losing_layers(
    instance: Path, led: ledger_mod.Ledger
) -> None:
    wabbajack.record_in_ledger(
        instance,
        instance / "x.compiler_settings",
        instance / "wabbajack" / "x.wabbajack",
        "68.0.0",
        compiled_at="2026-09-02T00:00:00+00:00",
    )
    data = json.loads((instance / ledger_mod.LEDGER_NAME).read_text(encoding="utf-8"))
    assert data["wabbajack"]["version"] == "68.0.0"
    assert data["wabbajack"]["compiled_at"] == "2026-09-02T00:00:00+00:00"
    assert data["layers"][0]["slug"] == "h2uqa3"
    assert "Collection Mod" in data["mods"]


# -- helpers -------------------------------------------------------------------------


class _Nullish:
    """A `Reporter` that keeps every line, so a failing test can show what was printed."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def stage(self, name: str, total: int | None = None) -> None:
        self.lines.append(f"stage {name}")

    def progress(self, done: int, total: int | None, label: str = "") -> None:
        return None

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def warn(self, msg: str) -> None:
        self.lines.append(f"warning: {msg}")

    def done(self, name: str, summary: str = "") -> None:
        self.lines.append(f"done {name}: {summary}")


def _args(instance: Path, **overrides: object):
    import argparse

    values = {
        "instance": str(instance),
        "name": None,
        "version": None,
        "author": None,
        "description": None,
        "website": None,
        "readme": None,
        "image": None,
        "output": None,
        "wabbajack_cli": None,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)
