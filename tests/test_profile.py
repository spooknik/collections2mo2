"""Tests for profile.py: mod ordering, separators, plugin order, INI helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from collections2wabbajack import profile

# --------------------------------------------------------- _compute_order / order_mods


def test_compute_order_sorts_by_phase_then_manifest_order():
    # Entries list order does NOT match phase order; phase must win, with
    # manifest/list position breaking ties within a phase.
    manifest = {"mods": [], "modRules": []}
    entries = [
        {"folder": "ModC", "phase": 1},
        {"folder": "ModA", "phase": 0},
        {"folder": "ModD", "phase": 666},
        {"folder": "ModB", "phase": 0},
    ]
    report = profile._compute_order(manifest, entries)
    assert report.folders == ["ModA", "ModB", "ModC", "ModD"]
    assert report.order_idx == [1, 3, 0, 2]
    assert profile.order_mods(manifest, entries) == ["ModA", "ModB", "ModC", "ModD"]


def test_compute_order_before_after_rules_applied():
    manifest = {
        "mods": [
            {"name": "E", "source": {"md5": "md5E"}},
            {"name": "F", "source": {"md5": "md5F"}},
        ],
        "modRules": [
            {"type": "before", "source": {"fileMD5": "md5F"}, "reference": {"fileMD5": "md5E"}},
        ],
    }
    entries = [
        {"folder": "E", "phase": 0, "md5": "md5E"},
        {"folder": "F", "phase": 0, "md5": "md5F"},
    ]
    report = profile._compute_order(manifest, entries)
    # Natural (base) order would be E, F; the rule forces F before E.
    assert report.folders == ["F", "E"]
    assert report.rules_applied == 1
    assert report.rules_ignored == 0


def test_compute_order_unresolvable_rule_is_ignored():
    manifest = {
        "mods": [
            {"name": "E", "source": {"md5": "md5E"}},
            {"name": "F", "source": {"md5": "md5F"}},
        ],
        "modRules": [
            {"type": "before", "source": {"fileMD5": "md5-unknown"}, "reference": {"fileMD5": "md5E"}},
        ],
    }
    entries = [
        {"folder": "E", "phase": 0, "md5": "md5E"},
        {"folder": "F", "phase": 0, "md5": "md5F"},
    ]
    report = profile._compute_order(manifest, entries)
    assert report.folders == ["E", "F"]
    assert report.rules_ignored == 1
    assert report.rules_applied == 0
    assert report.warnings == []


def test_compute_order_cycle_is_broken_keeping_base_order():
    manifest = {
        "mods": [
            {"name": "G", "source": {"md5": "md5G"}},
            {"name": "H", "source": {"md5": "md5H"}},
        ],
        "modRules": [
            {"type": "before", "source": {"fileMD5": "md5G"}, "reference": {"fileMD5": "md5H"}},
            {"type": "before", "source": {"fileMD5": "md5H"}, "reference": {"fileMD5": "md5G"}},
        ],
    }
    entries = [
        {"folder": "G", "phase": 0, "md5": "md5G"},
        {"folder": "H", "phase": 0, "md5": "md5H"},
    ]
    report = profile._compute_order(manifest, entries)
    assert report.folders == ["G", "H"]  # base order kept
    assert report.cycle_breaks == 1
    assert any("cycle-break" in w for w in report.warnings)


# --------------------------------------------------------------- build_profile_order


def test_build_profile_order_one_separator_per_phase_even_when_split():
    # A rule pulled a phase-0 mod after a phase-1 mod, splitting phase 0 into two
    # runs; there should still be exactly one "Phase 0" separator.
    entries = [
        {"folder": "A0", "phase": 0},
        {"folder": "B1", "phase": 1},
        {"folder": "C0", "phase": 0},
    ]
    order_idx = [0, 1, 2]
    items = profile.build_profile_order(entries, order_idx, add_separators=True)
    kinds = [(i.kind, i.name) for i in items]
    assert kinds == [
        ("mod", "A0"),
        ("separator", "Phase 0_separator"),
        ("mod", "B1"),
        ("separator", "Phase 1_separator"),
        ("mod", "C0"),
    ]
    assert sum(1 for i in items if i.kind == "separator") == 2


def test_build_profile_order_no_separators_when_disabled():
    entries = [{"folder": "A0", "phase": 0}, {"folder": "B1", "phase": 1}]
    items = profile.build_profile_order(entries, [0, 1], add_separators=False)
    assert all(i.kind == "mod" for i in items)
    assert [i.name for i in items] == ["A0", "B1"]


# ------------------------------------------------------------------- render_modlist


def test_render_modlist_descending_order_and_dlc_lines():
    entries = [
        {"folder": "ModA", "phase": 0, "optional": False},
        {"folder": "ModB", "phase": 666, "optional": False},
    ]
    items = [
        profile.ProfileItem(kind="mod", name="ModA", entry_idx=0, phase=0),
        profile.ProfileItem(kind="mod", name="ModB", entry_idx=1, phase=666),
    ]
    text = profile.render_modlist(items, entries, disable_optional=False, game_name="SkyrimSE")
    lines = text.splitlines()
    assert lines[0] == profile.HEADER
    # reversed(items): ModB comes first (top-to-bottom = highest priority first)
    assert lines[1] == "+ModB"
    assert lines[2] == "+ModA"
    assert lines[-3:] == profile.DLC_LINES

    text_fo4 = profile.render_modlist(items, entries, disable_optional=False, game_name="Fallout4")
    assert not any(line.startswith("*DLC:") for line in text_fo4.splitlines())


def test_render_modlist_disable_optional_disables_phase_666_and_optional_flag():
    entries = [
        {"folder": "ModA", "phase": 0, "optional": False},
        {"folder": "ModB", "phase": 666, "optional": False},
        {"folder": "ModC", "phase": 0, "optional": True},
    ]
    items = [
        profile.ProfileItem(kind="mod", name="ModA", entry_idx=0, phase=0),
        profile.ProfileItem(kind="mod", name="ModB", entry_idx=1, phase=666),
        profile.ProfileItem(kind="mod", name="ModC", entry_idx=2, phase=0),
    ]
    text = profile.render_modlist(items, entries, disable_optional=True, game_name="Fallout4")
    lines = text.splitlines()[1:]
    assert "+ModA" in lines
    assert "-ModB" in lines
    assert "-ModC" in lines


# --------------------------------------------------------------- build_plugin_order


def test_build_plugin_order_manifest_order_with_extras_appended_and_warned():
    manifest = {
        "plugins": [
            {"name": "Base.esp", "enabled": True},
            {"name": "Mid.esp", "enabled": False},
        ]
    }
    entries = [
        {"folder": "ModA", "plugins": ["Base.esp", "Extra1.esp"]},
        {"folder": "ModB", "plugins": ["Mid.esp"]},
    ]
    order_idx = [0, 1]
    ordered, warnings, stats = profile.build_plugin_order(manifest, entries, order_idx, "SkyrimSE")
    assert ordered == [
        ("Base.esp", True),
        ("Mid.esp", False),
        ("Extra1.esp", True),
    ]
    assert stats == {"in_manifest": 2, "extra": 1}
    assert len(warnings) == 1
    assert "Extra1.esp" in warnings[0]
    assert "not in the manifest's" in warnings[0]


def test_build_plugin_order_excludes_base_game_masters():
    manifest = {"plugins": [{"name": "Skyrim.esm", "enabled": True}]}
    entries = [{"folder": "ModA", "plugins": ["Skyrim.esm", "MyMod.esp"]}]
    ordered, warnings, stats = profile.build_plugin_order(manifest, entries, [0], "SkyrimSE")
    names = [n for n, _ in ordered]
    assert "Skyrim.esm" not in names
    assert "MyMod.esp" in names


# ------------------------------------------------------------ render_plugins/loadorder


def test_render_plugins_and_loadorder_txt():
    order = [("Base.esp", True), ("Mid.esp", False)]
    plugins_txt = profile.render_plugins_txt(order)
    assert plugins_txt.splitlines() == [profile.HEADER, "*Base.esp", "Mid.esp"]
    loadorder_txt = profile.render_loadorder_txt(order)
    assert loadorder_txt.splitlines() == [profile.HEADER, "Base.esp", "Mid.esp"]


# --------------------------------------------------------------------- merge_ini_*


def test_merge_ini_values_overrides_key_appends_new_preserves_other_lines(tmp_path: Path):
    target = tmp_path / "SkyrimPrefs.ini"
    target.write_text(
        "[Display]\niSize W=1920\n; a comment\niSize H=1080\n\n[General]\nfoo=bar\n",
        encoding="utf-8",
    )
    applied = profile.merge_ini_values(target, "Display", {"iSize W": "2560", "NewKey": "val"})
    assert applied == 2
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "iSize W=2560" in lines
    assert "iSize W=1920" not in lines
    assert "; a comment" in lines
    assert "iSize H=1080" in lines
    assert "NewKey=val" in lines
    assert "[General]" in lines
    assert "foo=bar" in lines
    # NewKey must land inside [Display], before [General]
    assert lines.index("NewKey=val") < lines.index("[General]")


def test_merge_ini_values_creates_missing_section(tmp_path: Path):
    target = tmp_path / "Fresh.ini"
    applied = profile.merge_ini_values(target, "Display", {"iSize W": "800"})
    assert applied == 1
    text = target.read_text(encoding="utf-8")
    assert "[Display]" in text
    assert "iSize W=800" in text


def test_merge_ini_tweak_applies_all_sections(tmp_path: Path):
    target = tmp_path / "SkyrimPrefs.ini"
    target.write_text("[Display]\niSize W=1920\niSize H=1080\n", encoding="utf-8")
    tweak = tmp_path / "MyTweak [SkyrimPrefs].ini"
    tweak.write_text("[Display]\niSize W=3440\niSize H=1440\n", encoding="utf-8")
    applied = profile.merge_ini_tweak(target, tweak)
    assert applied == 2
    text = target.read_text(encoding="utf-8")
    assert "iSize W=3440" in text.splitlines()
    assert "iSize H=1440" in text.splitlines()


# ---------------------------------------------------------------- apply_display_settings


def test_apply_display_settings_resolution_vsync_window_combo(tmp_path: Path):
    notes = profile.apply_display_settings(
        tmp_path, "SkyrimSE", resolution="3440x1440", vsync="off", window="windowed"
    )
    text = (tmp_path / "SkyrimPrefs.ini").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "iSize W=3440" in lines
    assert "iSize H=1440" in lines
    assert "iVSyncPresentInterval=0" in lines
    assert "bFull Screen=0" in lines
    assert "bBorderless=0" in lines
    assert len(notes) == 1
    assert "3440x1440" in notes[0]
    assert "vsync off" in notes[0]
    assert "window windowed" in notes[0]


def test_apply_display_settings_keep_does_not_touch_resolution(tmp_path: Path):
    notes = profile.apply_display_settings(
        tmp_path, "SkyrimSE", resolution="keep", vsync="on", window="fullscreen"
    )
    text = (tmp_path / "SkyrimPrefs.ini").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "iSize W" not in text
    assert "iVSyncPresentInterval=1" in lines
    assert "bFull Screen=1" in lines
    assert "bBorderless=0" in lines
    assert "kept" in notes[0]


def test_apply_display_settings_unknown_game_returns_nothing(tmp_path: Path):
    notes = profile.apply_display_settings(tmp_path, "NotAGame", "keep", "keep", "keep")
    assert notes == []
    assert list(tmp_path.iterdir()) == []


def test_parse_resolution_arg_validates():
    assert profile._parse_resolution_arg("auto") == "auto"
    assert profile._parse_resolution_arg("keep") == "keep"
    assert profile._parse_resolution_arg("1920x1080") == "1920x1080"
    with pytest.raises(Exception):
        profile._parse_resolution_arg("not-a-resolution")


# --------------------------------------------------------------------- render_mo2_ini


def test_render_mo2_ini_gamepath_bytearray_form():
    game_path = "D:/Games/Skyrim"
    ini = profile.render_mo2_ini("SkyrimSE", game_path, "Default", "2.5.2", [])
    lines = ini.splitlines()
    expected_native = game_path.replace("/", "\\").replace("\\", "\\\\")
    assert f"gamePath=@ByteArray({expected_native})" in lines
    assert "gameName=Skyrim Special Edition" in lines
    assert "selected_profile=@ByteArray(Default)" in lines
    assert "version=2.5.2" in lines
    assert "game_edition=Steam" in lines


def test_render_mo2_ini_empty_gamepath():
    ini = profile.render_mo2_ini("SkyrimSE", "", "Default", "2.5.2", [])
    assert "gamePath=" in ini.splitlines()


def test_render_mo2_ini_custom_executables_block():
    exe_blocks = [
        {
            "title": "SKSE",
            "binary": "D:/Games/Skyrim/skse64_loader.exe",
            "arguments": "",
            "workingDirectory": "D:/Games/Skyrim",
            "hide": "false",
            "ownicon": "true",
            "steamAppID": "",
            "toolbar": "true",
        }
    ]
    ini = profile.render_mo2_ini("SkyrimSE", "D:/Games/Skyrim", "Default", "2.5.2", exe_blocks)
    lines = ini.splitlines()
    assert "size=1" in lines
    assert "1\\title=SKSE" in lines
    assert "1\\binary=D:/Games/Skyrim/skse64_loader.exe" in lines
    assert "1\\toolbar=true" in lines
