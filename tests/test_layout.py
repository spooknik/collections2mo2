"""Tests for layout.plan_layout(): the 14 layout-normalisation cases from the
layout fix (see layout.py's module docstring and CLAUDE.md's "Non-obvious facts").
"""

from __future__ import annotations

import json

from collections2wabbajack import layout

# 1. root Data -----------------------------------------------------------------


def test_root_data_stays_in_place():
    files = ["meshes/armor.nif", "textures/armor.dds"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [
        ("meshes/armor.nif", "meshes/armor.nif"),
        ("textures/armor.dds", "textures/armor.dds"),
    ]


# 2. Data/ wrapper ---------------------------------------------------------------


def test_data_wrapper_is_unwrapped():
    files = ["Data/meshes/armor.nif", "Data/textures/armor.dds"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data_wrapped"
    assert plan.files == [
        ("Data/meshes/armor.nif", "meshes/armor.nif"),
        ("Data/textures/armor.dds", "textures/armor.dds"),
    ]


# 3. single wrapper -> SKSE/Plugins -----------------------------------------------


def test_single_wrapper_unwraps_to_skse_plugins():
    files = ["MyMod/SKSE/Plugins/mymod.dll"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "single_folder"
    assert plan.files == [("MyMod/SKSE/Plugins/mymod.dll", "SKSE/Plugins/mymod.dll")]


# 4-6. Nemesis_Engine / MapMarkers / Shaders are Data content, not wrappers ------


def test_nemesis_engine_is_root_data_not_a_wrapper():
    files = ["Nemesis_Engine/mod/mymod/mymod.txt"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [("Nemesis_Engine/mod/mymod/mymod.txt", "Nemesis_Engine/mod/mymod/mymod.txt")]


def test_mapmarkers_is_root_data_not_a_wrapper():
    files = ["MapMarkers/markers.json"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [("MapMarkers/markers.json", "MapMarkers/markers.json")]


def test_shaders_is_root_data_not_a_wrapper():
    files = ["Shaders/community.ini"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [("Shaders/community.ini", "Shaders/community.ini")]


# 7. junk __MACOSX + loose .ini -> data, junk dropped -----------------------------


def test_junk_macosx_dropped_loose_ini_makes_it_data():
    files = ["__MACOSX/._config.ini", "config.ini"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [("config.ini", "config.ini")]
    assert not any("__MACOSX" in w or "MACOSX" in w for w in plan.warnings)


# 8. loose ini beside a folder -> data, both kept --------------------------------


def test_loose_ini_beside_data_folder_both_kept():
    files = ["Settings.ini", "Textures/foo.dds"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data"
    assert plan.files == [
        ("Settings.ini", "Settings.ini"),
        ("Textures/foo.dds", "Textures/foo.dds"),
    ]


# 9. unknown single folder dead end -> as_is, root preserved --------------------


def test_unknown_single_folder_dead_end_is_as_is():
    files = ["MyStuff/readme.txt", "MyStuff/random.xyz"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "as_is"
    assert plan.files == [
        ("MyStuff/readme.txt", "MyStuff/readme.txt"),
        ("MyStuff/random.xyz", "MyStuff/random.xyz"),
    ]
    assert any("could not identify a Data folder" in w for w in plan.warnings)
    assert any("MyStuff" in w for w in plan.warnings)


# 10. dinput SKSE wrapper: Root/ loader + Scripts/ data, src/ dropped -----------


def test_dinput_skse_wrapper_splits_root_and_data_drops_src():
    files = [
        "skse64_2_02_06/skse64_loader.exe",
        "skse64_2_02_06/skse64_steam_loader.dll",
        "skse64_2_02_06/Data/Scripts/Source/foo.psc",
        "skse64_2_02_06/src/whatever.cpp",
    ]
    plan = layout.plan_layout(files, mod_type="dinput")
    assert plan.strategy == "data_wrapped"
    assert set(plan.files) == {
        ("skse64_2_02_06/Data/Scripts/Source/foo.psc", "Scripts/Source/foo.psc"),
        ("skse64_2_02_06/skse64_loader.exe", "Root/skse64_loader.exe"),
        ("skse64_2_02_06/skse64_steam_loader.dll", "Root/skse64_steam_loader.dll"),
    }
    assert len(plan.files) == 3
    assert not any("src" in w for w in plan.warnings)


# 11. dinput bare dll -> root -----------------------------------------------------


def test_dinput_bare_dll_goes_to_root():
    files = ["d3dcompiler_47.dll"]
    plan = layout.plan_layout(files, mod_type="dinput")
    assert plan.strategy == "root"
    assert plan.files == [("d3dcompiler_47.dll", "Root/d3dcompiler_47.dll")]


# 12. dinput Runtime-Swapper-like -> ALL under Root/, structure preserved -------


def test_dinput_runtime_swapper_like_all_under_root_preserving_structure():
    files = [
        "version.dll",
        "SomeGame.exe",
        "RuntimeSwap/manifest.json",
        "RuntimeSwap/patches/forward/a.hdiff",
    ]
    plan = layout.plan_layout(files, mod_type="dinput")
    assert plan.strategy == "root"
    assert plan.files == [
        ("version.dll", "Root/version.dll"),
        ("SomeGame.exe", "Root/SomeGame.exe"),
        ("RuntimeSwap/manifest.json", "Root/RuntimeSwap/manifest.json"),
        ("RuntimeSwap/patches/forward/a.hdiff", "Root/RuntimeSwap/patches/forward/a.hdiff"),
    ]


# 13. fomod dir beside content ---------------------------------------------------


def test_fomod_dir_beside_content_is_excluded():
    files = ["fomod/ModuleConfig.xml", "Data/meshes/armor.nif"]
    plan = layout.plan_layout(files)
    assert plan.strategy == "data_wrapped"
    assert plan.files == [("Data/meshes/armor.nif", "meshes/armor.nif")]
    assert all("fomod" not in src.lower() for src, _dst in plan.files)


# 14. vortex_override_instructions.json copy list -------------------------------


def test_override_json_copy_list_data_prefix_vs_root():
    files = [
        "vortex_override_instructions.json",
        "content/foo.esp",
        "content/bar.dll",
    ]
    override = [
        {"type": "copy", "source": "content/foo.esp", "destination": "Data\\foo.esp"},
        {"type": "copy", "source": "content/bar.dll", "destination": "bar.dll"},
    ]
    override_text = json.dumps(override)

    def read_text(path: str) -> str:
        assert path == "vortex_override_instructions.json"
        return override_text

    plan = layout.plan_layout(files, read_text=read_text)
    assert plan.strategy == "override_json"
    assert plan.files == [
        ("content/foo.esp", "foo.esp"),
        ("content/bar.dll", "Root/bar.dll"),
    ]
    assert plan.warnings == []
