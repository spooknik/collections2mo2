"""Tests for fomod.evaluate() -- choice replay and defaults-mode selection.

Fixtures live under tests/fixtures/fomod/<name>/ModuleConfig.xml, each paired with a
recorded-choices JSON in Vortex's format (mod["choices"]).
"""

from __future__ import annotations

import json
from pathlib import Path

from collections2mo2 import fomod

FIXTURES = Path(__file__).parent / "fixtures" / "fomod"


def _load_choices(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- jbo-like


def test_jbo_replay_positional_and_visibility():
    config = FIXTURES / "jbo" / "ModuleConfig.xml"
    choices = _load_choices(FIXTURES / "jbo" / "choices.json")

    plan = fomod.evaluate(config, choices=choices)

    assert plan.selections == [
        ("Stage", "Type", ["TypeA"]),
        ("Stage", "Extras", ["Extra1", "Extra2"]),
        ("Stage", "AOptions", ["Opt1", "Opt2"]),
    ]
    # The two invisible "Stage" steps (BOptions gated on Type=B, HiddenGroup gated
    # on an Or of two flags that never get set) contributed no selections and no
    # flags -- in particular SecretFlag, which would only be set if the hidden
    # group's plugin were ever chosen.
    assert plan.flags == {"Type": "A", "Chosen": "Yes"}
    assert "SecretFlag" not in plan.flags

    assert plan.files == [
        ("Base", ""),
        ("TypeA/a.esp", "a.esp"),
        ("Extra1/e1.esp", "e1.esp"),
        ("Extra2/e2.esp", "e2.esp"),
        ("Opt1/o1.esp", "o1.esp"),
        ("Opt2/o2.esp", "o2.esp"),
        # priority=10 folder sorts after every priority=0 file despite being
        # declared (and recorded) before them.
        ("Override", ""),
    ]
    assert plan.warnings == []
    assert plan.resolved_deps == 0
    assert plan.unknown_deps == 0


def test_jbo_defaults_select_exactly_one_picks_first_select_all_picks_all():
    config = FIXTURES / "jbo" / "ModuleConfig.xml"

    plan = fomod.evaluate(config, choices=None)

    assert plan.selections == [
        ("Stage", "Type", ["TypeA"]),
        ("Stage", "Extras", ["Extra1", "Extra2"]),
        ("Stage", "AOptions", []),
    ]
    assert plan.flags == {"Type": "A", "Chosen": "Yes"}
    assert plan.files == [
        ("Base", ""),
        ("TypeA/a.esp", "a.esp"),
        ("Extra1/e1.esp", "e1.esp"),
        ("Extra2/e2.esp", "e2.esp"),
        ("Override", ""),
    ]
    assert (
        "step 'Stage' group 'Type': SelectExactlyOne with nothing recommended; "
        "picked 'TypeA'" in plan.warnings
    )
    assert "default choice -- step 'Stage' group 'Type' [SelectExactlyOne]: TypeA" in plan.warnings
    assert (
        "default choice -- step 'Stage' group 'Extras' [SelectAll]: Extra1, Extra2" in plan.warnings
    )
    assert "default choice -- step 'Stage' group 'AOptions' [SelectAny]: (nothing)" in plan.warnings
    # The two invisible steps never produced a "default choice" line.
    assert not any("BOptions" in w or "HiddenGroup" in w for w in plan.warnings)


# --------------------------------------------------------------------- mcm-like


def test_mcm_defaults_flag_dependency_marks_recommended():
    config = FIXTURES / "mcm" / "ModuleConfig.xml"

    plan = fomod.evaluate(config, choices=None)

    assert plan.selections == [
        ("Init", "InitFlags", ["SetHasSKSE"]),
        ("Settings", "Preset", ["PresetA"]),
    ]
    assert plan.flags == {"HasSKSE": "Installed"}
    assert plan.files == [
        ("MCM/mcm_core.dll", "SKSE/Plugins/mcm_core.dll"),
        ("PresetA/a.esp", "Data/PresetA.esp"),
    ]
    assert plan.resolved_deps == 0
    assert plan.unknown_deps == 0


def test_mcm_replay_recorded_pick_overrides_recommended_type():
    config = FIXTURES / "mcm" / "ModuleConfig.xml"
    choices = _load_choices(FIXTURES / "mcm" / "choices_presetB.json")

    plan = fomod.evaluate(config, choices=choices)

    # The curator picked PresetB even though PresetA resolves to Recommended once
    # HasSKSE is set; the recorded pick wins.
    assert plan.selections == [
        ("Init", "InitFlags", ["SetHasSKSE"]),
        ("Settings", "Preset", ["PresetB"]),
    ]
    assert plan.files == [
        ("MCM/mcm_core.dll", "SKSE/Plugins/mcm_core.dll"),
        ("PresetB/b.esp", "b.esp"),
    ]
    assert plan.warnings == []


# ---------------------------------------------------------------- cc-patch-like


def test_cc_patch_recorded_pick_installed_despite_missing_resolver():
    """(i) recorded picks are installed even when the resolver returns Missing."""
    config = FIXTURES / "cc_patch" / "ModuleConfig.xml"
    choices = _load_choices(FIXTURES / "cc_patch" / "choices.json")

    plan = fomod.evaluate(config, choices=choices, file_state=None)

    assert plan.selections == [("Patches", "Patches", ["PatchA"])]
    assert plan.files == [("PatchA/pa.esp", "pa.esp")]
    # Both plugins' fileDependency patterns were evaluated (and unresolved), but
    # since the curator's pick and our computed pick agree there is nothing to
    # warn about.
    assert plan.resolved_deps == 0
    assert plan.unknown_deps == 2
    assert plan.warnings == []


def test_cc_patch_recorded_pick_resolves_cleanly_with_active_resolver():
    """(ii) with a resolver returning Active they resolve without warnings."""
    config = FIXTURES / "cc_patch" / "ModuleConfig.xml"
    choices = _load_choices(FIXTURES / "cc_patch" / "choices.json")

    plan = fomod.evaluate(config, choices=choices, file_state=lambda name: "Active")

    assert plan.selections == [("Patches", "Patches", ["PatchA"])]
    assert plan.files == [("PatchA/pa.esp", "pa.esp")]
    assert plan.resolved_deps == 2
    assert plan.unknown_deps == 0
    assert plan.warnings == []


def test_cc_patch_defaults_no_resolver_installs_nothing():
    """(iii) defaults mode with no resolver installs nothing from that group."""
    config = FIXTURES / "cc_patch" / "ModuleConfig.xml"

    plan = fomod.evaluate(config, choices=None, file_state=None)

    assert plan.selections == [("Patches", "Patches", [])]
    assert plan.files == []
    assert plan.unknown_deps == 2
    assert (
        "default choice -- step 'Patches' group 'Patches' [SelectAny]: (nothing)" in plan.warnings
    )
    # Defaults mode folds the group's unresolved fileDependency checks into the
    # trailing summary line (unlike replay mode, which only surfaces them when
    # they actually changed the outcome).
    assert (
        "0 fileDependency checks resolved via manifest/game/installed; 2 still unknown"
        in plan.warnings
    )


def test_cc_patch_defaults_active_resolver_installs_recommended():
    """(iii) ...and with an Active resolver installs the Recommended ones."""
    config = FIXTURES / "cc_patch" / "ModuleConfig.xml"

    plan = fomod.evaluate(config, choices=None, file_state=lambda name: "Active")

    assert plan.selections == [("Patches", "Patches", ["PatchA", "PatchB"])]
    assert plan.files == [("PatchA/pa.esp", "pa.esp"), ("PatchB/pb.esp", "pb.esp")]
    assert plan.resolved_deps == 2
    assert plan.unknown_deps == 0
    assert plan.warnings == [
        "default choice -- step 'Patches' group 'Patches' [SelectAny]: PatchA, PatchB"
    ]
