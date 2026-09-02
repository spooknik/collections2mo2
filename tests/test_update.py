"""Tests for `c2wj update`: the manifest delta, user-mod detection, the ledger bump.

All synthetic: two hand-built manifests and a `mods/` folder or two, so nothing here
touches the network, an archive or an MO2 instance. The end-to-end behaviour of the
command itself is verified against the real h2uqa3 collection (see README).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from collections2wabbajack import ledger, update


def _mod(
    name: str,
    *,
    tag: str,
    mod_id: int,
    file_id: int,
    md5: str,
    phase: int = 0,
    optional: bool = False,
    size: int = 1000,
    choices: dict | None = None,
    version: str = "1.0",
) -> dict:
    mod: dict = {
        "name": name,
        "phase": phase,
        "optional": optional,
        "version": version,
        "source": {
            "type": "nexus",
            "tag": tag,
            "modId": mod_id,
            "fileId": file_id,
            "md5": md5,
            "fileSize": size,
            "logicalFilename": name,
        },
    }
    if choices is not None:
        mod["choices"] = choices
    return mod


def _manifest(mods: list[dict], instructions: str = "") -> dict:
    return {"info": {"name": "Test List", "installInstructions": instructions}, "mods": mods}


def _by_name(deltas: list[update.ModDelta]) -> dict[str, update.ModDelta]:
    return {d.name: d for d in deltas}


# ------------------------------------------------------------------- classification


def test_diff_classifies_every_kind_of_change():
    # Vortex re-issues `source.tag` on every revision, so none of the tags match and the
    # pairing has to fall through to (modId, fileId) / md5 / modId.
    old = _manifest(
        [
            _mod("Untouched", tag="a1", mod_id=1, file_id=10, md5="md5-1"),
            _mod("New File", tag="a2", mod_id=2, file_id=20, md5="md5-2"),
            _mod("New Answers", tag="a3", mod_id=3, file_id=30, md5="md5-3", choices={"o": 1}),
            _mod("Goes Away", tag="a4", mod_id=4, file_id=40, md5="md5-4"),
        ]
    )
    new = _manifest(
        [
            _mod("Untouched", tag="b1", mod_id=1, file_id=10, md5="md5-1"),
            _mod("New File", tag="b2", mod_id=2, file_id=21, md5="md5-2b"),
            _mod("New Answers", tag="b3", mod_id=3, file_id=30, md5="md5-3", choices={"o": 2}),
            _mod("Brand New", tag="b5", mod_id=5, file_id=50, md5="md5-5"),
        ]
    )
    diff = update.diff_manifests(old, new)

    assert diff.counts() == {"unchanged": 1, "changed": 2, "added": 1, "removed": 1}
    assert [d.name for d in diff.unchanged] == ["Untouched"]
    assert [d.name for d in diff.added] == ["Brand New"]
    assert [d.name for d in diff.removed] == ["Goes Away"]

    changed = _by_name(diff.changed)
    assert changed["New File"].needs_install
    assert any("file 20 -> 21" in r for r in changed["New File"].reasons)
    assert changed["New Answers"].needs_install
    assert "FOMOD choices changed" in changed["New Answers"].reasons
    # Only the two changed mods and the added one are downloaded and reinstalled.
    assert diff.install_tags == {"b2", "b3", "b5"}


def test_diff_matches_by_modid_when_the_file_and_the_hash_both_change():
    old = _manifest([_mod("SKSE", tag="a", mod_id=30379, file_id=1, md5="old")])
    new = _manifest([_mod("SKSE", tag="b", mod_id=30379, file_id=2, md5="new")])
    diff = update.diff_manifests(old, new)
    assert diff.counts()["changed"] == 1
    assert diff.counts() == {"unchanged": 0, "changed": 1, "added": 0, "removed": 0}


def test_diff_keeps_two_files_of_the_same_mod_apart():
    # A curator may list one Nexus mod twice with two different files; each old entry
    # may only be claimed once, so this must not collapse into one pair plus an add.
    old = _manifest(
        [
            _mod("Part 1", tag="a1", mod_id=7, file_id=1, md5="m1"),
            _mod("Part 2", tag="a2", mod_id=7, file_id=2, md5="m2"),
        ]
    )
    new = _manifest(
        [
            _mod("Part 1", tag="b1", mod_id=7, file_id=1, md5="m1"),
            _mod("Part 2", tag="b2", mod_id=7, file_id=3, md5="m3"),
        ]
    )
    diff = update.diff_manifests(old, new)
    assert diff.counts() == {"unchanged": 1, "changed": 1, "added": 0, "removed": 0}
    assert diff.changed[0].name == "Part 2"


def test_a_metadata_only_change_costs_no_reinstall():
    old = _manifest([_mod("Mod", tag="a", mod_id=1, file_id=1, md5="m", phase=0)])
    new = _manifest([_mod("Mod", tag="b", mod_id=1, file_id=1, md5="m", phase=2, optional=True)])
    diff = update.diff_manifests(old, new)
    delta = diff.changed[0]
    assert not delta.needs_install
    assert diff.install_tags == set()
    assert "phase 0 -> 2" in delta.reasons
    assert any("optional" in r for r in delta.reasons)


def test_a_renamed_mod_is_changed_and_carries_both_folder_names():
    old = _manifest([_mod("Horizon Fix AE", tag="a", mod_id=9, file_id=1, md5="m")])
    new = _manifest([_mod("Horizon Fix", tag="b", mod_id=9, file_id=1, md5="m")])
    diff = update.diff_manifests(old, new, old_folders={"a": "Horizon Fix AE"})
    delta = diff.changed[0]
    assert delta.renamed
    assert (delta.old_folder, delta.new_folder) == ("Horizon Fix AE", "Horizon Fix")
    # A pure rename is a folder rename, not a re-download.
    assert not delta.needs_install
    assert diff.install_tags == set()


def test_a_folder_another_layer_owns_pushes_the_new_file_to_its_own_folder():
    # `taken` is the folders this layer does *not* solely own. A differing archive under
    # a name an add-on layer also owns has to go somewhere else.
    old = _manifest([_mod("SKSE64", tag="a", mod_id=1, file_id=1, md5="shared")])
    new = _manifest([_mod("SKSE64", tag="b", mod_id=1, file_id=2, md5="different")])
    diff = update.diff_manifests(
        old, new, old_folders={"a": "SKSE64"}, taken={"SKSE64": "shared"}, suffix="xk05aw"
    )
    delta = diff.changed[0]
    assert delta.new_folder == "SKSE64 ~xk05aw"
    assert delta.needs_install


def test_download_size_counts_only_what_has_to_be_fetched():
    old = _manifest([_mod("A", tag="a1", mod_id=1, file_id=1, md5="m1", size=100)])
    new = _manifest(
        [
            _mod("A", tag="b1", mod_id=1, file_id=1, md5="m1", size=100),
            _mod("B", tag="b2", mod_id=2, file_id=2, md5="m2", size=250),
        ]
    )
    diff = update.diff_manifests(old, new)
    assert diff.download_bytes == 250


# ------------------------------------------------------------- user-modified folders


def test_extra_files_mark_a_folder_as_user_modified(tmp_path: Path):
    mod_dir = tmp_path / "A Mod"
    mod_dir.mkdir()
    (mod_dir / "one.esp").write_text("", encoding="utf-8")
    (mod_dir / "meta.ini").write_text("[General]\n", encoding="utf-8")
    assert update.looks_user_modified(mod_dir, {"file_count": 1}) == ""

    (mod_dir / "my-edit.esp").write_text("", encoding="utf-8")
    why = update.looks_user_modified(mod_dir, {"file_count": 1})
    assert "more than the install recorded" in why


def test_a_file_newer_than_the_install_marks_a_folder_as_user_modified(tmp_path: Path):
    mod_dir = tmp_path / "A Mod"
    mod_dir.mkdir()
    plugin = mod_dir / "one.esp"
    plugin.write_text("", encoding="utf-8")
    # Extraction restores the archive's own (old) timestamps, so an untouched mod folder
    # is full of files older than the moment the layer was installed.
    old_time = time.time() - 3600
    os.utime(plugin, (old_time, old_time))
    assert update.looks_user_modified(mod_dir, {"file_count": 1}, "2999-01-01T00:00:00+00:00") == ""

    now = time.time()
    os.utime(plugin, (now, now))
    why = update.looks_user_modified(mod_dir, {"file_count": 1}, "2000-01-01T00:00:00+00:00")
    assert "modified after" in why


def test_meta_ini_alone_never_marks_a_folder_as_modified(tmp_path: Path):
    # Every install and every owner re-stamp rewrites meta.ini, so it is always "new".
    mod_dir = tmp_path / "A Mod"
    mod_dir.mkdir()
    (mod_dir / "one.esp").write_text("", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(mod_dir / "one.esp", (old_time, old_time))
    (mod_dir / "meta.ini").write_text("[General]\ncomments=owner: collection:a@2\n", encoding="utf-8")
    assert update.looks_user_modified(mod_dir, {"file_count": 1}, "2999-01-01T00:00:00+00:00") == ""


def test_missing_files_are_not_a_reason_to_keep_a_folder(tmp_path: Path):
    mod_dir = tmp_path / "A Mod"
    mod_dir.mkdir()
    assert update.looks_user_modified(mod_dir, {"file_count": 5}) == ""


# ------------------------------------------------------------------ the ledger bump


def test_update_layer_revision_keeps_position_and_records_the_previous_revision(tmp_path: Path):
    led = ledger.Ledger(tmp_path / "inst")
    led.register_layer("base", 66, name="Base", profile="TestProfile", manifest="c2wj/a.json")
    led.register_layer("addon", 3, name="Add On")
    led.data["layers"][0]["separators"] = ["Phase 0_separator"]

    layer = led.update_layer_revision(
        "base",
        66,
        68,
        name="Base",
        manifest="c2wj/collections/base/68/archive/collection.json",
        files={"install": "c2wj/base-68.install.json"},
    )
    assert layer is not None
    assert [entry["slug"] for entry in led.data["layers"]] == ["base", "addon"]
    assert led.data["layers"][0]["revision"] == 68
    assert led.data["layers"][0]["previous_revisions"] == [66]
    assert led.data["layers"][0]["profile"] == "TestProfile"
    assert led.data["layers"][0]["separators"] == ["Phase 0_separator"]
    assert led.data["layers"][0]["updated"]
    assert led.layer_owner(led.data["layers"][0]) == "collection:base@68"
    assert led.layer("base", 66) is None

    led.update_layer_revision("base", 68, 69)
    assert led.data["layers"][0]["previous_revisions"] == [66, 68]


def test_update_layer_revision_is_a_no_op_for_an_unknown_layer(tmp_path: Path):
    led = ledger.Ledger(tmp_path / "inst")
    led.register_layer("base", 1)
    assert led.update_layer_revision("nope", 1, 2) is None


def test_register_layer_does_not_lose_an_update_history(tmp_path: Path):
    led = ledger.Ledger(tmp_path / "inst")
    led.register_layer("base", 66)
    led.update_layer_revision("base", 66, 68)
    led.register_layer("base", 68, name="Base")  # a later `create`/`add` refresh
    assert led.data["layers"][0]["previous_revisions"] == [66]


def test_the_owner_hand_over_keeps_a_folder_shared_with_another_layer(tmp_path: Path):
    led = ledger.Ledger(tmp_path / "inst")
    led.set_mod_owner("SKSE", "collection:base@66", md5="m")
    led.add_mod_owner("SKSE", "collection:addon@2")

    led.remove_mod_owner("SKSE", "collection:base@66")
    led.set_mod_owner("SKSE", "collection:base@68", md5="m")
    led.add_mod_owner("SKSE", "collection:addon@2")
    assert led.owners_of("SKSE") == ["collection:base@68", "collection:addon@2"]


# ------------------------------------------------------------------ the printed plan


def test_render_plan_shows_counts_names_changelog_and_download_size():
    old = _manifest(
        [
            _mod("Untouched", tag="a1", mod_id=1, file_id=1, md5="m1"),
            _mod("Goes Away", tag="a2", mod_id=2, file_id=2, md5="m2"),
        ],
        instructions="Run LOOT afterwards.",
    )
    new = _manifest(
        [
            _mod("Untouched", tag="b1", mod_id=1, file_id=1, md5="m1"),
            _mod("Brand New", tag="b3", mod_id=3, file_id=3, md5="m3", size=2_500_000),
        ],
        instructions="Run LOOT, then Nemesis.",
    )
    diff = update.diff_manifests(old, new)
    lines = update.render_plan(
        diff,
        slug="h2uqa3",
        name="Test List",
        old_revision=66,
        new_revision=68,
        latest_revision=68,
        changelog={"revisionNumber": 68, "description": "Added a mod.", "createdAt": "2026-08-25"},
        instructions_diff=update._instructions_diff(old, new, 66, 68),
        keep_notes={"Goes Away": "(kept: also owned by collection:addon@2)"},
    )
    text = "\n".join(lines)
    assert "installed:   revision 66" in text
    assert "target:      revision 68 (latest published)" in text
    assert "1 unchanged, 0 changed, 1 added, 1 removed" in text
    assert "+ Brand New" in text
    assert "- Goes Away" in text
    assert "(kept: also owned by collection:addon@2)" in text
    assert "Added a mod." in text
    assert "install instructions changed:" in text
    assert "download:    1 archive(s), up to 2.5 MB" in text


def test_render_plan_names_a_rename_by_both_folders():
    old = _manifest([_mod("Old Name", tag="a", mod_id=1, file_id=1, md5="m")])
    new = _manifest([_mod("New Name", tag="b", mod_id=1, file_id=1, md5="m")])
    diff = update.diff_manifests(old, new, old_folders={"a": "Old Name"})
    text = "\n".join(
        update.render_plan(diff, slug="s", name="n", old_revision=1, new_revision=2)
    )
    assert "~ Old Name -> New Name" in text
    assert "rename:      1 mod folder(s)" in text


def test_instructions_diff_is_empty_when_the_text_is_unchanged():
    same = _manifest([], instructions="Same text.")
    assert update._instructions_diff(same, same, 1, 2) == []


# ------------------------------------------------------------ what happens to a folder


def _renamed_diff() -> update.ManifestDiff:
    old = _manifest([_mod("Old Name", tag="a", mod_id=1, file_id=1, md5="m")])
    new = _manifest([_mod("New Name", tag="b", mod_id=1, file_id=1, md5="m")])
    return update.diff_manifests(old, new, old_folders={"a": "Old Name"})


def test_a_folder_this_layer_alone_owns_is_renamed(tmp_path: Path):
    (tmp_path / "Old Name").mkdir()
    diff = _renamed_diff()
    update.plan_folder_actions(diff, tmp_path, {"Old Name"})
    assert diff.changed[0].folder_action == "rename"
    assert not diff.changed[0].needs_install


def test_a_folder_another_layer_owns_is_left_alone_and_the_mod_installed_fresh(tmp_path: Path):
    (tmp_path / "Old Name").mkdir()
    diff = _renamed_diff()
    update.plan_folder_actions(diff, tmp_path, set())
    delta = diff.changed[0]
    assert delta.folder_action == "release-old"
    assert delta.needs_install
    assert diff.install_tags == {"b"}


def test_a_renamed_mod_that_also_changed_file_drops_its_old_folder(tmp_path: Path):
    (tmp_path / "Old Name").mkdir()
    old = _manifest([_mod("Old Name", tag="a", mod_id=1, file_id=1, md5="m1")])
    new = _manifest([_mod("New Name", tag="b", mod_id=1, file_id=2, md5="m2")])
    diff = update.diff_manifests(old, new, old_folders={"a": "Old Name"})
    update.plan_folder_actions(diff, tmp_path, {"Old Name"})
    assert diff.changed[0].folder_action == "drop-old"
    assert diff.install_tags == {"b"}


def test_a_missing_old_folder_falls_back_to_a_fresh_install(tmp_path: Path):
    diff = _renamed_diff()  # nothing on disk
    update.plan_folder_actions(diff, tmp_path, {"Old Name"})
    assert diff.changed[0].folder_action == "release-old"
    assert diff.changed[0].needs_install
