"""Tests for collection layering: `c2wj add` / `c2wj remove` and layered profiles.

Everything here works on a synthetic instance built by `make_instance` -- a ledger,
per-layer `install.json`, manifests and empty `mods/` folders -- so no network, no
archives and no MO2 are involved. `keep_inis=True` is passed wherever the test is not
about INIs, so the profile writer does not go looking in the developer's real
`Documents/My Games` for seed files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collections2wabbajack import create, layers, ledger, naming, profile
from collections2wabbajack.reporter import NullReporter

# --------------------------------------------------------------------- fixtures


def _entry(folder: str, md5: str, *, phase: int = 0, plugins: list[str] | None = None) -> dict:
    return {
        "name": folder,
        "folder": folder,
        "tag": folder.lower(),
        "md5": md5,
        "phase": phase,
        "optional": False,
        "install_mode": "fresh",
        "strategy": "data",
        "plugins": list(plugins or []),
    }


def _mod(name: str, md5: str, *, phase: int = 0) -> dict:
    return {"name": name, "phase": phase, "source": {"tag": name.lower(), "md5": md5}}


def make_instance(tmp_path: Path, specs: list[dict]) -> tuple[Path, ledger.Ledger]:
    """Build an instance whose ledger has one layer per spec, with mods/ folders on disk."""
    inst = tmp_path / "inst"
    (inst / "mods").mkdir(parents=True, exist_ok=True)
    (inst / "c2wj").mkdir(parents=True, exist_ok=True)
    led = ledger.Ledger(inst)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE", source_path=str(tmp_path))
    led.set_mo2(version="2.5.2")
    for spec in specs:
        slug, rev = spec["slug"], spec["revision"]
        rel_manifest = f"c2wj/collections/{slug}/{rev}/archive/collection.json"
        manifest_path = inst / rel_manifest
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(spec["manifest"]), encoding="utf-8")

        rel_install = f"c2wj/{slug}-{rev}.install.json"
        (inst / rel_install).write_text(
            json.dumps(
                {
                    "manifest": str(manifest_path),
                    "mods_dir": str(inst / "mods"),
                    "game_name": "SkyrimSE",
                    "entries": spec["entries"],
                }
            ),
            encoding="utf-8",
        )
        led.register_layer(
            slug,
            rev,
            name=spec.get("name") or slug,
            profile=specs[0].get("profile") or "TestProfile",
            manifest=rel_manifest,
            files={"install": rel_install, "downloads": f"c2wj/{slug}-{rev}.downloads.json"},
        )
        owner = ledger.collection_owner(slug, rev)
        for entry in spec["entries"]:
            (inst / "mods" / entry["folder"]).mkdir(parents=True, exist_ok=True)
            if entry["folder"] in led.data["mods"]:
                led.add_mod_owner(entry["folder"], owner)
            else:
                led.set_mod_owner(entry["folder"], owner, md5=entry["md5"])
    led.save()
    return inst, led


# ------------------------------------------------------- cross-layer rule ordering


def test_layer_blocks_stay_contiguous_without_cross_layer_rules(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "modRules": []},
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            },
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {"mods": [_mod("X", "md5x")], "modRules": []},
                "entries": [_entry("X", "md5x")],
            },
        ],
    )
    layer_objs, warnings = profile.load_layers(inst, led)
    assert warnings == []
    order = profile.compute_layered_order(layer_objs)
    assert order.folders == ["A", "B", "X"]
    assert order.layer_crossings == 0
    # The add-on gets its own separator, the base keeps phase separators.
    assert order.sep_names == ["Phase 0_separator", "Phase 0_separator", "Add On_separator"]


def test_cross_layer_rule_pulls_a_base_mod_after_an_addon_mod(tmp_path: Path):
    # The add-on's manifest says "X loads before B", and B belongs to the base layer.
    # The rule resolves by md5 across the layers, so X has to end up before B even
    # though that breaks the tidy base-then-add-on blocks.
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "modRules": []},
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            },
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {
                    "mods": [_mod("X", "md5x")],
                    "modRules": [
                        {
                            "type": "before",
                            "source": {"fileMD5": "md5x"},
                            "reference": {"fileMD5": "md5b"},
                        }
                    ],
                },
                "entries": [_entry("X", "md5x")],
            },
        ],
    )
    layer_objs, _ = profile.load_layers(inst, led)
    order = profile.compute_layered_order(layer_objs)
    assert order.folders == ["A", "X", "B"]
    assert order.rules.applied == 1
    assert order.layer_crossings == 1
    assert any("cross-layer rule moved" in w for w in order.warnings)


def test_shared_folder_is_listed_once_and_attributed_to_the_first_layer(tmp_path: Path):
    shared = _entry("SKSE", "md5skse")
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("SKSE", "md5skse"), _mod("A", "md5a")]},
                "entries": [shared, _entry("A", "md5a")],
            },
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {"mods": [_mod("SKSE", "md5skse"), _mod("X", "md5x")]},
                "entries": [dict(shared), _entry("X", "md5x")],
            },
        ],
    )
    layer_objs, _ = profile.load_layers(inst, led)
    order = profile.compute_layered_order(layer_objs)
    assert order.folders.count("SKSE") == 1
    assert order.shared["SKSE"] == [0, 1]
    assert led.owners_of("SKSE") == ["collection:base@1", "collection:addon@2"]
    # The shared folder sits in the base block, not the add-on's.
    assert order.sep_names[order.folders.index("SKSE")] == "Phase 0_separator"


# ------------------------------------------------------------- folder collisions


def test_assign_folder_names_shares_a_folder_when_the_md5_matches():
    taken = {"SKSE64": "md5skse", "Other Mod": "md5other"}
    mods = [
        {"name": "SKSE64", "source": {"tag": "t1", "md5": "md5skse"}},
        {"name": "Brand New", "source": {"tag": "t2", "md5": "md5new"}},
    ]
    result = naming.assign_folder_names(mods, taken=taken, suffix="xk05aw")
    assert result == {"t1": "SKSE64", "t2": "Brand New"}


def test_assign_folder_names_suffixes_when_the_md5_differs():
    taken = {"SKSE64": "md5skse"}
    mods = [{"name": "SKSE64", "source": {"tag": "t1", "md5": "a-different-file"}}]
    result = naming.assign_folder_names(mods, taken=taken, suffix="xk05aw")
    assert result == {"t1": "SKSE64 ~xk05aw"}


def test_assign_folder_names_ignores_taken_without_a_suffix():
    # The base layer passes no suffix: its own names must come out exactly as before.
    taken = {"SKSE64": "a-different-file"}
    mods = [{"name": "SKSE64", "source": {"tag": "t1", "md5": "md5skse"}}]
    assert naming.assign_folder_names(mods, taken=taken) == {"t1": "SKSE64"}


def test_layer_suffix_still_respects_the_folder_name_cap():
    long_name = "L" * 100
    taken = {naming.sanitize_folder_name(long_name): "a-different-file"}
    mods = [{"name": long_name, "source": {"tag": "t1", "md5": "md5new"}}]
    folder = naming.assign_folder_names(mods, taken=taken, suffix="xk05aw")["t1"]
    assert len(folder) <= naming.MAX_FOLDER_NAME
    assert folder.endswith(" ~xk05aw")


# --------------------------------------------------------- user mods in the profile


def test_user_mod_keeps_its_top_position_in_modlist(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "info": {}},
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            }
        ],
    )
    (inst / "mods" / "My Test Mod").mkdir()
    profile_dir = inst / "profiles" / "TestProfile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text(
        "# This file was automatically generated by Mod Organizer.\n"
        "+My Test Mod\n-Phase 0_separator\n+B\n+A\n",
        encoding="utf-8",
    )

    report = profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    lines = (profile_dir / "modlist.txt").read_text(encoding="utf-8").splitlines()
    assert lines[1] == "+My Test Mod"
    assert report["user_mods"] == ["My Test Mod"]
    assert "My Test Mod" not in report["mod_order"]


def test_user_mod_keeps_its_middle_position_relative_to_neighbours(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "info": {}},
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            }
        ],
    )
    (inst / "mods" / "Mine").mkdir()
    profile_dir = inst / "profiles" / "TestProfile"
    profile_dir.mkdir(parents=True)
    # Descending priority: B, then the user's mod, then A at the bottom.
    (profile_dir / "modlist.txt").write_text(
        "# header\n-Phase 0_separator\n+B\n+Mine\n+A\n", encoding="utf-8"
    )

    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    rows = [name for _, name in profile.read_marked_lines(profile_dir / "modlist.txt")]
    assert rows.index("B") < rows.index("Mine") < rows.index("A")


def test_new_instance_puts_user_mods_at_the_top(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a")], "info": {}},
                "entries": [_entry("A", "md5a")],
            }
        ],
    )
    (inst / "mods" / "Hand Installed").mkdir()
    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    lines = (inst / "profiles" / "TestProfile" / "modlist.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert lines[1] == "+Hand Installed"


def test_user_mod_at_the_top_stays_above_a_newly_added_layer(tmp_path: Path):
    # The regression that showed up on the real instance: the add-on's block goes in at
    # the top of the modlist, and a user mod pinned above everything must stay above it
    # rather than being anchored to the base mod that used to be its neighbour.
    base_spec = {
        "slug": "base",
        "revision": 1,
        "name": "Base List",
        "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "info": {}},
        "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
    }
    inst, _base_led = make_instance(tmp_path, [base_spec])
    (inst / "mods" / "My Test Mod").mkdir()
    profile_dir = inst / "profiles" / "TestProfile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text(
        "# header\n+My Test Mod\n-Phase 0_separator\n+B\n+A\n", encoding="utf-8"
    )

    _, led2 = make_instance(
        tmp_path,
        [
            base_spec,
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {"mods": [_mod("X", "md5x")], "info": {}},
                "entries": [_entry("X", "md5x")],
            },
        ],
    )
    profile.render_instance(
        inst, led=led2, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    lines = (profile_dir / "modlist.txt").read_text(encoding="utf-8").splitlines()
    assert lines[1] == "+My Test Mod"
    assert lines[2] == "-Add On_separator"
    assert lines[3] == "+X"


def test_user_mod_plugins_keep_their_state(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {
                    "mods": [_mod("A", "md5a")],
                    "plugins": [{"name": "A.esp", "enabled": True}],
                    "info": {},
                },
                "entries": [_entry("A", "md5a", plugins=["A.esp"])],
            }
        ],
    )
    user = inst / "mods" / "Mine"
    user.mkdir()
    (user / "Mine.esp").write_text("", encoding="utf-8")
    profile_dir = inst / "profiles" / "TestProfile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "plugins.txt").write_text("# header\n*A.esp\nMine.esp\n", encoding="utf-8")

    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    rows = profile.read_marked_lines(profile_dir / "plugins.txt")
    assert ("*", "A.esp") in rows
    # disabled in the old file, still disabled now
    assert ("", "Mine.esp") in rows


def test_tool_mod_is_placed_above_the_collection_and_below_user_mods(tmp_path: Path):
    # What `c2wj tools install dyndolod` produces: a mod folder owned by `tool:<id>`,
    # not by any layer. It must land in modlist.txt above the collection's top block
    # (highest layer priority) but below anything the user pinned above everything.
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a"), _mod("B", "md5b")], "info": {}},
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            }
        ],
    )
    (inst / "mods" / "My Test Mod").mkdir()
    (inst / "mods" / "DynDOLOD Resources SE").mkdir()
    led.set_mod_owner("DynDOLOD Resources SE", ledger.tool_owner("dyndolod"))
    led.save()

    report = profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    rows = [
        name
        for _, name in profile.read_marked_lines(inst / "profiles" / "TestProfile" / "modlist.txt")
        if not name.startswith("DLC: ")
    ]
    # Top of file = highest priority: user mod, then the tool mod, then the collection.
    assert rows == ["My Test Mod", "DynDOLOD Resources SE", "Phase 0_separator", "B", "A"]
    assert report["tool_mods"] == ["DynDOLOD Resources SE"]
    assert "DynDOLOD Resources SE" not in report["user_mods"]
    assert "DynDOLOD Resources SE" not in report["mod_order"]


def test_tool_mod_survives_re_render_at_its_managed_position(tmp_path: Path):
    # Once rendered, the tool mod is a *managed* row (marker driven by the renderer,
    # not preserved-from-old-file like a user mod) -- re-rendering must not move it.
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a")], "info": {}},
                "entries": [_entry("A", "md5a")],
            }
        ],
    )
    (inst / "mods" / "DynDOLOD Resources SE").mkdir()
    led.set_mod_owner("DynDOLOD Resources SE", ledger.tool_owner("dyndolod"))
    led.save()

    profile.render_instance(inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile")
    profile.render_instance(inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile")
    rows = [
        name
        for _, name in profile.read_marked_lines(inst / "profiles" / "TestProfile" / "modlist.txt")
        if not name.startswith("DLC: ")
    ]
    assert rows == ["DynDOLOD Resources SE", "Phase 0_separator", "A"]


# -------------------------------------------------------- SSE Display Tweaks override


def test_render_instance_generates_sse_display_tweaks_override_at_top_priority(tmp_path: Path):
    # ModHigh (higher priority: nearer the top of modlist.txt) and ModLow both ship
    # SSEDisplayTweaks.ini. ModHigh's copy is the one that should be used as the base
    # for the override -- and refreshing must not touch ModLow's.
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {
                    "mods": [_mod("ModLow", "md5lo"), _mod("ModHigh", "md5hi")],
                    "info": {},
                },
                "entries": [_entry("ModLow", "md5lo"), _entry("ModHigh", "md5hi")],
            }
        ],
    )
    for name, resolution in (("ModLow", "1280x720"), ("ModHigh", "2048x1152")):
        tweaks = inst / "mods" / name / "SKSE" / "Plugins"
        tweaks.mkdir(parents=True)
        (tweaks / "SSEDisplayTweaks.ini").write_text(
            f"[Render]\nResolution = {resolution}\n\n"
            "[Fullscreen]\nFullscreen = false\nBorderless = true\n\n"
            "[VSync]\nEnableVSync = false\n",
            encoding="utf-8",
        )

    report = profile.render_instance(
        inst,
        led=led,
        reporter=NullReporter(),
        profile_name="TestProfile",
        resolution="2560x1440",
        vsync="off",
        window="borderless",
    )
    profile_dir = inst / "profiles" / "TestProfile"
    rows = [name for _, name in profile.read_marked_lines(profile_dir / "modlist.txt")]
    assert rows[0] == profile.DISPLAY_OVERRIDE_MOD_NAME

    override_ini = (
        inst / "mods" / profile.DISPLAY_OVERRIDE_MOD_NAME / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini"
    )
    text = override_ini.read_text(encoding="utf-8")
    assert "Resolution=2560x1440" in text  # from ModHigh (2048x1152), not ModLow (1280x720)
    assert "Borderless=true" in text
    assert "EnableVSync=false" in text

    low_ini = (inst / "mods" / "ModLow" / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini").read_text(
        encoding="utf-8"
    )
    assert "Resolution = 1280x720" in low_ini  # untouched

    mods = led.data["mods"][profile.DISPLAY_OVERRIDE_MOD_NAME]
    assert mods["owner"] == ledger.USER_OWNER
    assert mods["generated_by"] == profile.DISPLAY_OVERRIDE_MARKER
    assert profile.DISPLAY_OVERRIDE_MOD_NAME not in report["mod_order"]


def test_render_instance_keep_display_settings_does_not_create_override(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("ModA", "md5a")], "info": {}},
                "entries": [_entry("ModA", "md5a")],
            }
        ],
    )
    tweaks = inst / "mods" / "ModA" / "SKSE" / "Plugins"
    tweaks.mkdir(parents=True)
    (tweaks / "SSEDisplayTweaks.ini").write_text(
        "[Render]\nResolution = 2048x1152\n\n"
        "[Fullscreen]\nFullscreen = false\nBorderless = true\n\n"
        "[VSync]\nEnableVSync = false\n",
        encoding="utf-8",
    )

    profile.render_instance(inst, led=led, reporter=NullReporter(), profile_name="TestProfile")
    assert not (inst / "mods" / profile.DISPLAY_OVERRIDE_MOD_NAME).exists()
    assert profile.DISPLAY_OVERRIDE_MOD_NAME not in led.data["mods"]


def test_render_instance_refreshes_override_from_changed_base_ini_and_stays_on_top(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("ModA", "md5a")], "info": {}},
                "entries": [_entry("ModA", "md5a")],
            }
        ],
    )
    tweaks = inst / "mods" / "ModA" / "SKSE" / "Plugins"
    tweaks.mkdir(parents=True)
    ini_path = tweaks / "SSEDisplayTweaks.ini"
    ini_path.write_text(
        "[Render]\nResolution = 1920x1080\n\n"
        "[Fullscreen]\nFullscreen = true\nBorderless = false\n\n"
        "[VSync]\nEnableVSync = true\n",
        encoding="utf-8",
    )
    profile.render_instance(
        inst,
        led=led,
        reporter=NullReporter(),
        profile_name="TestProfile",
        resolution="3440x1440",
        vsync="on",
        window="windowed",
    )

    # A collection update changes the base file's own settings.
    ini_path.write_text(
        "[Render]\nResolution = 800x600\n\n"
        "[Fullscreen]\nFullscreen = false\nBorderless = true\n\n"
        "[VSync]\nEnableVSync = false\n",
        encoding="utf-8",
    )
    (inst / "mods" / "My Own Mod").mkdir()  # a user mod -- still below the override, which
    # has to be *the* top row (not merely above the collection) to reliably win MO2's
    # virtual-filesystem merge against any SSEDisplayTweaks.ini a user mod might also ship.
    profile.render_instance(
        inst,
        led=led,
        reporter=NullReporter(),
        profile_name="TestProfile",
        resolution="3440x1440",
        vsync="on",
        window="windowed",
    )

    profile_dir = inst / "profiles" / "TestProfile"
    rows = [name for _, name in profile.read_marked_lines(profile_dir / "modlist.txt")]
    assert rows[0] == profile.DISPLAY_OVERRIDE_MOD_NAME
    assert "My Own Mod" in rows

    override_ini = (
        inst / "mods" / profile.DISPLAY_OVERRIDE_MOD_NAME / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini"
    )
    text = override_ini.read_text(encoding="utf-8")
    # The user's own choice (windowed) still wins...
    assert "Fullscreen=false" in text
    assert "Borderless=false" in text
    # ...but the resolution requested is fixed, and unrelated keys still come from the
    # refreshed base file (the point being that the override mod is one folder, kept in
    # sync, not a stale one-time snapshot).
    assert "Resolution=3440x1440" in text


def test_splice_preserved_puts_a_run_of_user_mods_back_in_order():
    new = ["A", "B", "C"]
    old = ["A", "U1", "U2", "B", "C"]
    assert profile.splice_preserved(new, old, ["U1", "U2"]) == ["A", "U1", "U2", "B", "C"]


def test_splice_preserved_bottom_of_list_has_no_predecessor():
    assert profile.splice_preserved(["A", "B"], ["U", "A", "B"], ["U"]) == ["U", "A", "B"]


# --------------------------------------------------------------------- INI reverts


def test_merge_ini_values_records_the_value_it_replaced(tmp_path: Path):
    target = tmp_path / "Skyrim.ini"
    target.write_text("[General]\nsLanguage=ENGLISH\n", encoding="utf-8")
    record: dict = {}
    profile.merge_ini_values(target, "General", {"sLanguage": "GERMAN", "bNew": "1"}, record)
    assert record["sLanguage"] == {"value": "GERMAN", "previous": "ENGLISH"}
    assert record["bNew"] == {"value": "1", "previous": None}


def test_restore_ini_values_puts_back_previous_and_deletes_new_keys(tmp_path: Path):
    target = tmp_path / "Skyrim.ini"
    target.write_text("[General]\nsLanguage=ENGLISH\n", encoding="utf-8")
    record: dict = {}
    profile.merge_ini_values(target, "General", {"sLanguage": "GERMAN", "bNew": "1"}, record)
    restored, deleted = profile.restore_ini_values(target, "General", record)
    assert (restored, deleted) == (1, 1)
    assert target.read_text(encoding="utf-8").splitlines() == ["[General]", "sLanguage=ENGLISH"]


def test_restore_ini_values_leaves_hand_edited_keys_alone(tmp_path: Path):
    target = tmp_path / "Skyrim.ini"
    target.write_text("[General]\nsLanguage=ENGLISH\n", encoding="utf-8")
    record: dict = {}
    profile.merge_ini_values(target, "General", {"sLanguage": "GERMAN"}, record)
    profile.merge_ini_values(target, "General", {"sLanguage": "FRENCH"})  # the user, later
    restored, deleted = profile.restore_ini_values(target, "General", record)
    assert (restored, deleted) == (0, 0)
    assert "sLanguage=FRENCH" in target.read_text(encoding="utf-8")


def test_ledger_keeps_the_first_previous_value_across_re_renders():
    led = ledger.Ledger(Path("."))
    led.record_ini_keys(
        "collection:a@1", {"Skyrim.ini": {"[General]": {"k": {"value": "2", "previous": "1"}}}}
    )
    # Re-rendering re-applies the tweak, which now sees its own value in place.
    led.record_ini_keys(
        "collection:a@1", {"Skyrim.ini": {"[General]": {"k": {"value": "2", "previous": "2"}}}}
    )
    assert led.ini_keys_of("collection:a@1")["Skyrim.ini"]["[General]"]["k"]["previous"] == "1"


def test_revert_ini_keys_restores_the_value_the_previous_layer_set(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    target = profile_dir / "Skyrim.ini"
    target.write_text("[Display]\niSize W=1920\n", encoding="utf-8")

    base_record: dict = {}
    profile.merge_ini_values(target, "Display", {"iSize W": "2560"}, base_record)
    addon_record: dict = {}
    profile.merge_ini_values(target, "Display", {"iSize W": "3440"}, addon_record)

    notes = profile.revert_ini_keys(profile_dir, {"Skyrim.ini": {"[Display]": addon_record}})
    assert notes and "1 restored" in notes[0]
    assert "iSize W=2560" in target.read_text(encoding="utf-8")


# -------------------------------------------------------------- custom executables

_INI = """[General]
gamePath=@ByteArray(D:\\\\Games\\\\Skyrim)

[customExecutables]
size=2
1\\title=SKSE
1\\binary=D:/Games/Skyrim/skse64_loader.exe
1\\arguments=
1\\workingDirectory=D:/Games/Skyrim
2\\title=PGPatcher
2\\binary=D:/Games/Skyrim/Data/PGPatcher/PGPatcher.exe
2\\arguments=
2\\workingDirectory=D:/Games/Skyrim

[Plugins]
RootBuilder\\hash=true
"""


def test_drop_executables_renumbers_and_keeps_other_sections(tmp_path: Path):
    ini = tmp_path / "ModOrganizer.ini"
    ini.write_text(_INI, encoding="utf-8")
    removed = layers.drop_executables(
        ini, {("d:/games/skyrim/data/pgpatcher/pgpatcher.exe", "")}
    )
    assert removed == ["PGPatcher"]
    text = ini.read_text(encoding="utf-8")
    assert "size=1" in text
    assert "1\\title=SKSE" in text
    assert "PGPatcher" not in text
    # MO2's own settings are untouched.
    assert "RootBuilder\\hash=true" in text
    assert "gamePath=@ByteArray(D:\\\\Games\\\\Skyrim)" in text


def test_drop_executables_keeps_mo2s_crlf_line_endings(tmp_path: Path):
    ini = tmp_path / "ModOrganizer.ini"
    ini.write_bytes(_INI.replace("\n", "\r\n").encode("utf-8"))
    layers.drop_executables(ini, {("d:/games/skyrim/data/pgpatcher/pgpatcher.exe", "")})
    raw = ini.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_drop_executables_is_a_no_op_when_nothing_matches(tmp_path: Path):
    ini = tmp_path / "ModOrganizer.ini"
    ini.write_text(_INI, encoding="utf-8")
    assert layers.drop_executables(ini, {("nope.exe", "")}) == []
    assert ini.read_text(encoding="utf-8") == _INI


# ------------------------------------------------------------------------- remove


def _remove_args(inst: Path, slug: str, **kwargs) -> argparse.Namespace:
    return argparse.Namespace(
        slug=slug,
        instance=str(inst),
        purge_downloads=kwargs.get("purge_downloads", False),
        force=kwargs.get("force", False),
    )


def test_remove_refuses_the_base_layer(tmp_path: Path):
    inst, _ = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a")], "info": {}},
                "entries": [_entry("A", "md5a")],
            }
        ],
    )
    rc = layers.cmd_remove(_remove_args(inst, "base"), NullReporter())
    assert rc == 2
    after = ledger.load(inst)
    assert [layer["slug"] for layer in after.data["layers"]] == ["base"]
    assert (inst / "mods" / "A").is_dir()


def test_remove_unknown_slug_is_an_error(tmp_path: Path):
    inst, _ = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "manifest": {"mods": [_mod("A", "md5a")], "info": {}},
                "entries": [_entry("A", "md5a")],
            }
        ],
    )
    assert layers.cmd_remove(_remove_args(inst, "nope"), NullReporter()) == 2


def test_remove_addon_deletes_only_its_own_folders(tmp_path: Path):
    shared = _entry("SKSE", "md5skse")
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("SKSE", "md5skse"), _mod("A", "md5a")], "info": {}},
                "entries": [shared, _entry("A", "md5a")],
            },
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {"mods": [_mod("SKSE", "md5skse"), _mod("X", "md5x")], "info": {}},
                "entries": [dict(shared), _entry("X", "md5x")],
            },
        ],
    )
    (inst / "mods" / "My Test Mod").mkdir()
    profile_dir = inst / "profiles" / "TestProfile"
    profile_dir.mkdir(parents=True)
    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    before = (profile_dir / "modlist.txt").read_text(encoding="utf-8")
    assert "Add On_separator" in before

    rc = layers.cmd_remove(_remove_args(inst, "addon"), NullReporter())
    assert rc == 0

    after = ledger.load(inst)
    assert [layer["slug"] for layer in after.data["layers"]] == ["base"]
    assert not (inst / "mods" / "X").exists()
    assert (inst / "mods" / "SKSE").is_dir()
    assert after.owners_of("SKSE") == ["collection:base@1"]
    assert (inst / "mods" / "My Test Mod").is_dir()
    assert not (inst / "mods" / "Add On_separator").exists()

    modlist = (profile_dir / "modlist.txt").read_text(encoding="utf-8")
    assert "Add On_separator" not in modlist
    assert "+X" not in modlist
    assert "+My Test Mod" in modlist
    assert not (inst / "c2wj" / "addon-2.install.json").exists()


def test_add_then_remove_round_trips_the_profile(tmp_path: Path):
    """modlist.txt and plugins.txt come back to what they were before the layer."""
    base_spec = {
        "slug": "base",
        "revision": 1,
        "name": "Base List",
        "manifest": {
            "mods": [_mod("A", "md5a"), _mod("B", "md5b")],
            "plugins": [{"name": "A.esp", "enabled": True}],
            "info": {},
        },
        "entries": [_entry("A", "md5a", plugins=["A.esp"]), _entry("B", "md5b")],
    }
    inst, led = make_instance(tmp_path, [base_spec])
    (inst / "mods" / "My Test Mod").mkdir()
    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    profile_dir = inst / "profiles" / "TestProfile"
    modlist_before = (profile_dir / "modlist.txt").read_text(encoding="utf-8")
    plugins_before = (profile_dir / "plugins.txt").read_text(encoding="utf-8")

    # Layer an add-on on by hand (add_layer's network stages are not under test here).
    inst2, led2 = make_instance(
        tmp_path,
        [
            base_spec,
            {
                "slug": "addon",
                "revision": 2,
                "name": "Add On",
                "manifest": {
                    "mods": [_mod("X", "md5x")],
                    "plugins": [{"name": "X.esp", "enabled": True}],
                    "info": {},
                },
                "entries": [_entry("X", "md5x", plugins=["X.esp"])],
            },
        ],
    )
    assert inst2 == inst
    profile.render_instance(
        inst, led=led2, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    assert "+X" in (profile_dir / "modlist.txt").read_text(encoding="utf-8")
    assert "*X.esp" in (profile_dir / "plugins.txt").read_text(encoding="utf-8")

    assert layers.cmd_remove(_remove_args(inst, "addon"), NullReporter()) == 0
    assert (profile_dir / "modlist.txt").read_text(encoding="utf-8") == modlist_before
    assert (profile_dir / "plugins.txt").read_text(encoding="utf-8") == plugins_before


# ------------------------------------------------------------- per-layer file names


def test_layer_paths_are_namespaced_per_collection(tmp_path: Path):
    paths = create.Paths(tmp_path / "inst")
    lp = create.LayerPaths(paths, "xk05aw", 7)
    assert lp.install_json.name == "xk05aw-7.install.json"
    assert lp.downloads_json.name == "xk05aw-7.downloads.json"
    assert lp.ledger_files()["install"] == "c2wj/xk05aw-7.install.json"


def test_migrate_instance_renames_pre_layering_stage_files(tmp_path: Path):
    inst = tmp_path / "inst"
    (inst / "c2wj").mkdir(parents=True)
    paths = create.Paths(inst)
    (inst / "c2wj" / "downloads.json").write_text('{"entries": []}', encoding="utf-8")
    (inst / "c2wj" / "inspect.json").write_text(
        json.dumps({"downloads_json": str(inst / "c2wj" / "downloads.json"), "entries": []}),
        encoding="utf-8",
    )
    (inst / "c2wj" / "install.json").write_text('{"entries": []}', encoding="utf-8")
    led = ledger.Ledger(inst)
    led.register_layer("h2uqa3", 68, name="Base")

    moved = create.migrate_instance(paths, led, NullReporter())
    assert len(moved) == 3
    assert (inst / "c2wj" / "h2uqa3-68.install.json").exists()
    assert not (inst / "c2wj" / "install.json").exists()
    inspected = json.loads((inst / "c2wj" / "h2uqa3-68.inspect.json").read_text(encoding="utf-8"))
    assert inspected["downloads_json"].endswith("h2uqa3-68.downloads.json")
    assert led.data["layers"][0]["files"]["install"] == "c2wj/h2uqa3-68.install.json"


def test_ledger_upgrade_from_schema_1(tmp_path: Path):
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / ledger.LEDGER_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "layers": [{"slug": "base", "revision": 1}],
                "mods": {"A": {"owner": "collection:base@1", "md5": "md5a"}},
                "ini_keys": {"collection:base@1": {"Skyrim.ini": {"[Display]": ["iSize W"]}}},
            }
        ),
        encoding="utf-8",
    )
    led = ledger.load(inst)
    assert led.data["version"] == ledger.VERSION
    assert led.data["mods"]["A"]["owners"] == ["collection:base@1"]
    keys = led.ini_keys_of("collection:base@1")
    assert keys["Skyrim.ini"]["[Display]"]["iSize W"] == {"value": None, "previous": None}
