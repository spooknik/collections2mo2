"""Tests for profile.py: mod ordering, separators, plugin order, INI helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from collections2wabbajack import ledger, profile

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
    ordered, _warnings, _stats = profile.build_plugin_order(manifest, entries, [0], "SkyrimSE")
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


def test_apply_display_settings_borderless_sets_bfullscreen_and_bborderless(tmp_path: Path):
    notes = profile.apply_display_settings(
        tmp_path, "SkyrimSE", resolution="keep", vsync="on", window="borderless"
    )
    text = (tmp_path / "SkyrimPrefs.ini").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "bFull Screen=1" in lines
    assert "bBorderless=1" in lines
    assert "iVSyncPresentInterval=1" in lines
    assert "window borderless" in notes[0]


def test_apply_display_settings_updates_every_duplicate_section(tmp_path: Path):
    # Regression: a real "My Games" SkyrimPrefs.ini can end up with [Display] duplicated
    # (BethINI / the vanilla launcher / hand edits append rather than merge). Bethesda's
    # own INI reader takes the *last* occurrence of a duplicate key, so if we only patch
    # the first [Display] block, a stale "bBorderless=0" left in a later block silently
    # wins in-game even though our summary line (and the first block) say "borderless".
    target = tmp_path / "SkyrimPrefs.ini"
    target.write_text(
        "[Display]\n"
        "iSize W=1920\n"
        "iSize H=1080\n"
        "bFull Screen=1\n"
        "bBorderless=0\n"
        "iVSyncPresentInterval=1\n"
        "\n"
        "[Controls]\n"
        "foo=bar\n"
        "\n"
        "[Display]\n"
        "bBorderless=0\n",
        encoding="utf-8",
    )
    profile.apply_display_settings(tmp_path, "SkyrimSE", resolution="keep", vsync="on", window="borderless")
    lines = target.read_text(encoding="utf-8").splitlines()
    # Every "bBorderless" occurrence -- including the stray one in the second [Display]
    # block, whichever a Bethesda-style parser reads last -- must agree.
    borderless_lines = [ln for ln in lines if ln.split("=", 1)[0].strip() == "bBorderless"]
    assert borderless_lines == ["bBorderless=1"] * len(borderless_lines)
    assert len(borderless_lines) == 2


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
    with pytest.raises(argparse.ArgumentTypeError):
        profile._parse_resolution_arg("not-a-resolution")


# -------------------------------------------------------- SSE Display Tweaks awareness


def test_find_winning_file_returns_highest_priority_enabled_mod(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    for name in ("ModLow", "ModHigh", "ModDisabled"):
        (mods_dir / name).mkdir(parents=True)
        (mods_dir / name / "thing.txt").write_text("x", encoding="utf-8")
    modlist = tmp_path / "modlist.txt"
    # Top-to-bottom = descending priority; ModDisabled is a "-" row and must be skipped.
    modlist.write_text(
        f"{profile.HEADER}\n-ModDisabled\n+ModHigh\n+ModLow\n", encoding="utf-8"
    )
    result = profile.find_winning_file(modlist, mods_dir, "thing.txt")
    assert result is not None
    name, path = result
    assert name == "ModHigh"
    assert path == mods_dir / "ModHigh" / "thing.txt"


def test_find_winning_file_none_when_nobody_ships_it(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    (mods_dir / "ModA").mkdir(parents=True)
    modlist = tmp_path / "modlist.txt"
    modlist.write_text(f"{profile.HEADER}\n+ModA\n", encoding="utf-8")
    assert profile.find_winning_file(modlist, mods_dir, "thing.txt") is None


def test_find_winning_file_skips_named_folder(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    for name in ("Override", "ModA"):
        (mods_dir / name).mkdir(parents=True)
        (mods_dir / name / "thing.txt").write_text("x", encoding="utf-8")
    modlist = tmp_path / "modlist.txt"
    modlist.write_text(f"{profile.HEADER}\n+Override\n+ModA\n", encoding="utf-8")
    result = profile.find_winning_file(modlist, mods_dir, "thing.txt", skip={"Override"})
    assert result is not None
    assert result[0] == "ModA"


def test_apply_sse_display_tweaks_override_keep_prints_and_creates_nothing(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    tweaks = mods_dir / "OnlyMod" / "SKSE" / "Plugins"
    tweaks.mkdir(parents=True)
    (tweaks / "SSEDisplayTweaks.ini").write_text(
        "[Render]\nResolution = 2048x1152\n\n"
        "[Fullscreen]\nFullscreen = false\nBorderless = true\n\n"
        "[VSync]\nEnableVSync = false\n",
        encoding="utf-8",
    )
    (profile_dir / "modlist.txt").write_text(f"{profile.HEADER}\n+OnlyMod\n", encoding="utf-8")

    class _FakeLedger:
        def __init__(self):
            self.data = {"mods": {}}

    notes = profile.apply_sse_display_tweaks_override(
        profile_dir, mods_dir, "SkyrimSE", "keep", "keep", "keep", _FakeLedger()
    )
    assert len(notes) == 1
    assert "OnlyMod" in notes[0]
    assert "2048x1152" in notes[0]
    assert "borderless" in notes[0]
    assert "vsync off" in notes[0]
    assert not (mods_dir / profile.DISPLAY_OVERRIDE_MOD_NAME).exists()


def test_apply_sse_display_tweaks_override_uncomments_keys_in_one_render_section(
    tmp_path: Path,
):
    # Regression: the real SSE Display Tweaks SKSE plugin keeps Resolution, Fullscreen,
    # Borderless *and* EnableVSync all inside one `[Render]` section, commented out by
    # default with example values (`#Fullscreen=false`) -- not the four-separate-active-
    # sections schema this code originally (wrongly) assumed. A key search that only
    # matches active (uncommented) lines, or that looks in the wrong section, silently
    # writes nothing and the requested override never takes effect in-game.
    mods_dir = tmp_path / "mods"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    tweaks = mods_dir / "SSE Display Tweaks" / "SKSE" / "Plugins"
    tweaks.mkdir(parents=True)
    (tweaks / "SSEDisplayTweaks.ini").write_text(
        "[Main]\nLogLevel=debug\n\n"
        "[Render]\n"
        "#Fullscreen=false\n"
        "#Borderless=true\n"
        "BorderlessUpscale=false\n"
        "#Resolution=1920x1080\n"
        "EnableVSync=true\n"
        "\n"
        "[Window]\nSomeOtherKey=1\n",
        encoding="utf-8",
    )
    (profile_dir / "modlist.txt").write_text(
        f"{profile.HEADER}\n+SSE Display Tweaks\n", encoding="utf-8"
    )

    class _FakeLedger:
        def __init__(self):
            self.data = {"mods": {}}

        def set_mod_owner(self, folder, owner, **kw):
            rec = {"owner": owner, "owners": [owner]}
            self.data["mods"][folder] = rec
            return rec

    profile.apply_sse_display_tweaks_override(
        profile_dir, mods_dir, "SkyrimSE", "2560x1440", "off", "borderless", _FakeLedger()
    )
    text = (
        mods_dir / profile.DISPLAY_OVERRIDE_MOD_NAME / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini"
    ).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "Resolution=2560x1440" in lines
    assert "Fullscreen=false" in lines
    assert "Borderless=true" in lines
    assert "EnableVSync=false" in lines
    # No stray commented-out duplicates of the keys we set are left behind.
    assert "#Fullscreen=false" not in lines
    assert "#Borderless=true" not in lines
    assert "#Resolution=1920x1080" not in lines
    # Untouched content survives.
    assert "BorderlessUpscale=false" in lines
    assert "SomeOtherKey=1" in lines


def test_apply_sse_display_tweaks_override_none_when_no_mod_ships_it(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    profile_dir = tmp_path / "profile"
    (mods_dir / "Plain").mkdir(parents=True)
    profile_dir.mkdir()
    (profile_dir / "modlist.txt").write_text(f"{profile.HEADER}\n+Plain\n", encoding="utf-8")

    class _FakeLedger:
        def __init__(self):
            self.data = {"mods": {}}

    notes = profile.apply_sse_display_tweaks_override(
        profile_dir, mods_dir, "SkyrimSE", "1920x1080", "on", "fullscreen", _FakeLedger()
    )
    assert notes == []


# ------------------------------------------------------------- display: ledger memory


def test_ledger_set_display_keep_never_clears(tmp_path: Path):
    led = ledger.Ledger(tmp_path)
    led.set_display(resolution="1920x1080", vsync="on", window="windowed")
    led.set_display()  # a no-op "everything keep" call
    assert led.data["display"]["resolution"] == "1920x1080"
    assert led.data["display"]["vsync"] == "on"
    assert led.data["display"]["window"] == "windowed"


def test_ledger_clear_display(tmp_path: Path):
    led = ledger.Ledger(tmp_path)
    led.set_display(resolution="1920x1080", vsync="on", window="windowed")
    led.clear_display()
    assert led.data["display"] == {
        "resolution": None,
        "vsync": None,
        "window": None,
        "updated": None,
    }


def test_resolve_effective_display_substitutes_only_keep_fields(tmp_path: Path):
    led = ledger.Ledger(tmp_path)
    led.set_display(resolution="2560x1440", vsync="on", window="borderless")
    resolution, vsync, window, remembered = profile._resolve_effective_display(
        led, "keep", "off", "keep"
    )
    assert resolution == "2560x1440"
    assert vsync == "off"  # explicit request, not the stored one
    assert window == "borderless"
    assert remembered == frozenset({"resolution", "window"})


def test_resolve_effective_display_fresh_ledger_keeps_keep(tmp_path: Path):
    led = ledger.Ledger(tmp_path)
    resolution, vsync, window, remembered = profile._resolve_effective_display(
        led, "keep", "keep", "keep"
    )
    assert (resolution, vsync, window) == ("keep", "keep", "keep")
    assert remembered == frozenset()


def test_persist_display_choice_stores_only_concrete_values(tmp_path: Path):
    led = ledger.Ledger(tmp_path)
    profile._persist_display_choice(led, "keep", "keep", "keep")
    assert led.data["display"] == {
        "resolution": None,
        "vsync": None,
        "window": None,
        "updated": None,
    }

    profile._persist_display_choice(led, "2560x1440", "on", "borderless")
    display = led.data["display"]
    assert display["resolution"] == "2560x1440"
    assert display["vsync"] == "on"
    assert display["window"] == "borderless"
    assert display["updated"] is not None


def test_apply_display_settings_remembered_annotation(tmp_path: Path):
    notes = profile.apply_display_settings(
        tmp_path,
        "SkyrimSE",
        "2560x1440",
        "on",
        "borderless",
        remembered=frozenset({"resolution"}),
    )
    assert "2560x1440 (remembered)" in notes[0]
    assert "vsync on (remembered)" not in notes[0]
    assert "window borderless (remembered)" not in notes[0]


def test_apply_sse_display_tweaks_override_keep_reports_override_in_effect(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    base_tweaks = mods_dir / "OnlyMod" / "SKSE" / "Plugins"
    base_tweaks.mkdir(parents=True)
    (base_tweaks / "SSEDisplayTweaks.ini").write_text(
        "[Render]\nResolution = 1920x1080\nFullscreen = false\nBorderless = false\n"
        "EnableVSync = false\n",
        encoding="utf-8",
    )
    override_dir = mods_dir / profile.DISPLAY_OVERRIDE_MOD_NAME / "SKSE" / "Plugins"
    override_dir.mkdir(parents=True)
    (override_dir / "SSEDisplayTweaks.ini").write_text(
        "[Render]\nResolution=2560x1440\nFullscreen=false\nBorderless=true\nEnableVSync=true\n",
        encoding="utf-8",
    )
    (profile_dir / "modlist.txt").write_text(
        f"{profile.HEADER}\n+OnlyMod\n+{profile.DISPLAY_OVERRIDE_MOD_NAME}\n", encoding="utf-8"
    )

    class _FakeLedger:
        def __init__(self):
            self.data = {
                "mods": {
                    profile.DISPLAY_OVERRIDE_MOD_NAME: {
                        "owner": "user",
                        "generated_by": profile.DISPLAY_OVERRIDE_MARKER,
                    }
                }
            }

    notes = profile.apply_sse_display_tweaks_override(
        profile_dir, mods_dir, "SkyrimSE", "keep", "keep", "keep", _FakeLedger()
    )
    assert len(notes) == 1
    assert "in effect" in notes[0]
    assert "2560x1440" in notes[0]
    assert "OnlyMod" in notes[0]
    assert "governs the display" not in notes[0]


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
