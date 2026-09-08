"""Tests for `c2mo2 update`: the manifest delta, user-mod detection, the ledger bump.

All synthetic: two hand-built manifests and a `mods/` folder or two, so nothing here
touches the network, an archive or an MO2 instance. The end-to-end behaviour of the
command itself is verified against the real h2uqa3 collection (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from collections2mo2 import create, ledger, update
from collections2mo2.reporter import NullReporter


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
    (mod_dir / "meta.ini").write_text(
        "[General]\ncomments=owner: collection:a@2\n", encoding="utf-8"
    )
    assert update.looks_user_modified(mod_dir, {"file_count": 1}, "2999-01-01T00:00:00+00:00") == ""


def test_missing_files_are_not_a_reason_to_keep_a_folder(tmp_path: Path):
    mod_dir = tmp_path / "A Mod"
    mod_dir.mkdir()
    assert update.looks_user_modified(mod_dir, {"file_count": 5}) == ""


# ------------------------------------------------------------------ the ledger bump


def test_update_layer_revision_keeps_position_and_records_the_previous_revision(tmp_path: Path):
    led = ledger.Ledger(tmp_path / "inst")
    led.register_layer("base", 66, name="Base", profile="TestProfile", manifest="c2mo2/a.json")
    led.register_layer("addon", 3, name="Add On")
    led.data["layers"][0]["separators"] = ["Phase 0_separator"]

    layer = led.update_layer_revision(
        "base",
        66,
        68,
        name="Base",
        manifest="c2mo2/collections/base/68/archive/collection.json",
        files={"install": "c2mo2/base-68.install.json"},
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
    text = "\n".join(update.render_plan(diff, slug="s", name="n", old_revision=1, new_revision=2))
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


# -- a custom archive store (`create --downloads-dir`) ---------------------------------


def test_downloaded_md5s_finds_archives_in_a_custom_downloads_dir(tmp_path: Path):
    inst = tmp_path / "inst"
    (inst / "c2mo2").mkdir(parents=True)
    custom = tmp_path / "archives"
    custom.mkdir()
    (custom / "a.7z").write_bytes(b"a")

    led = ledger.Ledger(inst)
    led.set_downloads_dir(custom)
    led.register_layer("base", 1, files={"downloads": "c2mo2/base-1.downloads.json"})
    led.save()
    (inst / "c2mo2" / "base-1.downloads.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"md5": "AABBCC", "path": str(custom / "a.7z")},
                    {"md5": "ddeeff", "path": str(custom / "gone.7z")},
                ]
            }
        ),
        encoding="utf-8",
    )

    paths = create.Paths.for_instance(inst)
    assert paths.downloads == custom.resolve()
    # Only the archive that is actually on disk counts, and md5s compare lowercased.
    assert update._downloaded_md5s(paths, led) == {"aabbcc"}


# ------------------------------------------ the whole command, with a failing stage


class _CollectingReporter(NullReporter):
    """A `NullReporter` that remembers the summary lines and the warnings."""

    def __init__(self):
        self.logs: list[str] = []
        self.warnings: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


class _Info:
    revision_number = 2
    name = "Test List"
    game = "skyrimspecialedition"


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def revision_info(self, ref, revision=None):
        return _Info()

    def latest_revision(self, ref):
        return 2

    def collection_changelog(self, ref, revision):
        return None


_OLD_MANIFEST = _manifest(
    [
        _mod("A", tag="a1", mod_id=1, file_id=1, md5="m1"),
        _mod("B", tag="a2", mod_id=2, file_id=2, md5="m2"),
    ]
)
_NEW_MANIFEST = _manifest(
    [
        _mod("A", tag="b1", mod_id=1, file_id=1, md5="m1"),
        _mod("B", tag="b2", mod_id=2, file_id=3, md5="m3"),
    ]
)


def _install_row(name: str, tag: str, md5: str, mod_id: int, file_id: int) -> dict:
    return {
        "name": name,
        "folder": name,
        "tag": tag,
        "md5": md5,
        "mod_id": mod_id,
        "file_id": file_id,
        "phase": 0,
        "optional": False,
        "install_mode": "fresh",
        "strategy": "data",
        "plugins": [],
        "file_count": 1,
    }


def _updatable_instance(tmp_path: Path) -> Path:
    """An instance with one layer at revision 1, two installed mods, no profile."""
    inst = tmp_path / "inst"
    (inst / "mods" / "A").mkdir(parents=True)
    (inst / "mods" / "B").mkdir(parents=True)
    (inst / "c2mo2").mkdir(parents=True)

    rel_manifest = "c2mo2/collections/base/1/archive/collection.json"
    manifest_path = inst / rel_manifest
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(_OLD_MANIFEST), encoding="utf-8")

    (inst / "c2mo2" / "base-1.install.json").write_text(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "mods_dir": str(inst / "mods"),
                "game_name": "SkyrimSE",
                "entries": [
                    _install_row("A", "a1", "m1", 1, 1),
                    _install_row("B", "a2", "m2", 2, 2),
                ],
            }
        ),
        encoding="utf-8",
    )
    (inst / "c2mo2" / "base-1.downloads.json").write_text(
        json.dumps({"entries": [{"tag": "a1", "md5": "m1"}, {"tag": "a2", "md5": "m2"}]}),
        encoding="utf-8",
    )
    (inst / "c2mo2" / "base-1.inspect.json").write_text(
        json.dumps({"entries": [{"tag": "a1"}, {"tag": "a2"}]}), encoding="utf-8"
    )

    led = ledger.Ledger(inst)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE", source_path=str(tmp_path))
    led.set_mo2(version="2.5.2")
    led.register_layer(
        "base",
        1,
        name="Test List",
        manifest=rel_manifest,
        files={
            "install": "c2mo2/base-1.install.json",
            "downloads": "c2mo2/base-1.downloads.json",
            "inspect": "c2mo2/base-1.inspect.json",
            "survey": "c2mo2/base-1.survey.json",
        },
    )
    for folder, md5 in (("A", "m1"), ("B", "m2")):
        led.set_mod_owner(folder, ledger.collection_owner("base", 1), md5=md5)
    led.save()
    return inst


def _stub_update(
    monkeypatch,
    *,
    downloads=None,
    download_rc=0,
    inspect=None,
    inspect_rc=0,
    install=None,
    install_rc=0,
):
    """Stub everything `cmd_update` reaches the network (or an archive) for."""

    def fake_fetch_manifest(client, ref, revision, collections_dir, info=None):
        path = Path(collections_dir) / "base" / "2" / "archive" / "collection.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_NEW_MANIFEST), encoding="utf-8")
        return info or _Info(), path

    def write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def fake_download(**kwargs):
        write(Path(kwargs["json_path"]), downloads or {"entries": [{"tag": "b2", "md5": "m3"}]})
        return download_rc

    def fake_inspect(ns, reporter=None):
        write(Path(ns.out), inspect or {"entries": [{"tag": "b2"}]})
        return inspect_rc

    def fake_install(ns, reporter=None):
        write(Path(ns.out), install or {"entries": []})
        return install_rc

    monkeypatch.setattr(update, "NexusClient", _FakeClient)
    monkeypatch.setattr(update, "fetch_manifest", fake_fetch_manifest)
    # Cosmetic Nexus-categories fetch; it has nothing to do with the failure gates.
    monkeypatch.setattr(update.categories, "prepare_layer", lambda *a, **kw: None)
    monkeypatch.setattr(update, "run_download", fake_download)
    monkeypatch.setattr(update.archive_inspect, "cmd_inspect", fake_inspect)
    monkeypatch.setattr(update.installer, "cmd_install", fake_install)
    monkeypatch.setattr(
        create, "render_profile", lambda *a, **kw: {"mod_order": ["A", "B"], "user_mods": []}
    )
    monkeypatch.setattr(update, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("NEXUS_API_KEY", "test-key")


def _update_args(inst: Path, **kwargs) -> argparse.Namespace:
    args = argparse.Namespace(
        instance=str(inst),
        layer=None,
        to=None,
        dry_run=False,
        yes=True,
        jobs=1,
        allow_missing=False,
        skip_errors=False,
        purge_old=False,
        choices_overrides=None,
    )
    for key, value in kwargs.items():
        setattr(args, key, value)
    return args


_DOWNLOAD_FAILED = {"entries": [{"tag": "b2", "name": "B", "status": "error", "error": "404"}]}
_INSTALL_FAILED = {
    "entries": [
        {
            "name": "B",
            "folder": "B",
            "tag": "b2",
            "md5": "m3",
            "strategy": "failed",
            "warnings": ["install failed: nothing extracted"],
        }
    ]
}


def _rows_by_name(inst: Path, revision: int) -> dict[str, dict]:
    data = json.loads(
        (inst / "c2mo2" / f"base-{revision}.install.json").read_text(encoding="utf-8")
    )
    return {row["name"]: row for row in data["entries"]}


def test_update_without_skip_errors_stops_on_a_failed_download(monkeypatch, tmp_path: Path):
    inst = _updatable_instance(tmp_path)
    _stub_update(monkeypatch, downloads=_DOWNLOAD_FAILED, download_rc=1)
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst), rep) == 1

    after = ledger.load(inst)
    assert after.data["layers"][0]["revision"] == 1
    assert not (inst / "c2mo2" / "base-2.install.json").exists()
    assert after.owners_of("B") == ["collection:base@1"]
    assert any("nothing was changed" in w for w in rep.warnings)


def test_update_with_skip_errors_keeps_the_old_row_for_an_undownloadable_mod(
    monkeypatch, tmp_path: Path
):
    inst = _updatable_instance(tmp_path)
    _stub_update(monkeypatch, downloads=_DOWNLOAD_FAILED, download_rc=1)
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst, skip_errors=True), rep) == 0

    rows = _rows_by_name(inst, 2)
    # B's new file never arrived, so the row still describes the revision-1 archive
    # that is actually unpacked in mods/B.
    assert rows["B"]["md5"] == "m2" and rows["B"]["file_id"] == 2
    assert rows["B"]["folder"] == "B"
    assert any("revision 1 archive is still installed" in w for w in rows["B"]["warnings"])
    assert rows["A"]["md5"] == "m1"

    after = ledger.load(inst)
    layer = after.data["layers"][0]
    assert layer["revision"] == 2
    assert after.layer_skipped(layer) == [{"name": "B", "stage": "download", "reason": "404"}]
    assert "  NOT installed (skipped): 1" in rep.logs
    assert "    B  [download] 404" in rep.logs
    assert any("are NOT in the instance" in w for w in rep.warnings)


def test_update_with_allow_missing_alone_also_carries_on_but_records_nothing_extra(
    monkeypatch, tmp_path: Path
):
    # --allow-missing forgives a file Nexus no longer serves, exactly like --skip-errors
    # does for this case; the mod is still listed as skipped.
    inst = _updatable_instance(tmp_path)
    _stub_update(monkeypatch, downloads=_DOWNLOAD_FAILED, download_rc=1)
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst, allow_missing=True), rep) == 0
    assert any("--allow-missing" in w for w in rep.warnings)
    assert ledger.load(inst).data["layers"][0]["revision"] == 2


def test_update_without_skip_errors_stops_on_a_failed_install(monkeypatch, tmp_path: Path):
    inst = _updatable_instance(tmp_path)
    _stub_update(monkeypatch, install=_INSTALL_FAILED, install_rc=1)
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst), rep) == 1
    assert ledger.load(inst).data["layers"][0]["revision"] == 1
    assert any("failed to install" in w for w in rep.warnings)


def test_update_with_skip_errors_keeps_the_old_row_for_a_failed_install(
    monkeypatch, tmp_path: Path
):
    inst = _updatable_instance(tmp_path)
    _stub_update(monkeypatch, install=_INSTALL_FAILED, install_rc=1)
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst, skip_errors=True), rep) == 0

    rows = _rows_by_name(inst, 2)
    assert rows["B"]["md5"] == "m2"  # the failed entry never replaces the old row
    assert any(
        "revision 2's file failed to install (--skip-errors)" in w
        and "mods/B is whatever revision 1 left there" in w
        for w in rows["B"]["warnings"]
    )
    after = ledger.load(inst)
    assert after.layer_skipped(after.data["layers"][0]) == [
        {"name": "B", "stage": "install", "reason": "install failed: nothing extracted"}
    ]
    assert "  NOT installed (skipped): 1" in rep.logs


def test_a_clean_update_records_no_skipped_mods(monkeypatch, tmp_path: Path):
    inst = _updatable_instance(tmp_path)
    _stub_update(
        monkeypatch,
        install={"entries": [_install_row("B", "b2", "m3", 2, 3)]},
    )
    rep = _CollectingReporter()

    assert update.cmd_update(_update_args(inst, skip_errors=True), rep) == 0
    rows = _rows_by_name(inst, 2)
    assert rows["B"]["md5"] == "m3"  # the new file did install
    after = ledger.load(inst)
    assert after.layer_skipped(after.data["layers"][0]) == []
    assert not any("NOT installed (skipped)" in line for line in rep.logs)


# ------------------------------------------------- `status` reads the skipped list


def test_status_lists_the_mods_the_last_run_skipped(monkeypatch, tmp_path: Path):
    inst = _updatable_instance(tmp_path)
    led = ledger.load(inst)
    led.set_layer_skipped(
        "base", 1, [{"name": "Gone Mod", "stage": "download", "reason": "404 Not Found"}]
    )
    led.save()
    monkeypatch.setattr(update, "load_dotenv", lambda *a, **kw: None)
    rep = _CollectingReporter()

    rc = update.cmd_status(argparse.Namespace(instance=str(inst), offline=True), rep)

    assert rc == 0
    assert "     NOT installed (skipped by the last run): 1" in rep.logs
    assert "       Gone Mod  [download] 404 Not Found" in rep.logs


def test_status_says_nothing_about_skips_for_a_clean_layer(monkeypatch, tmp_path: Path):
    inst = _updatable_instance(tmp_path)
    monkeypatch.setattr(update, "load_dotenv", lambda *a, **kw: None)
    rep = _CollectingReporter()

    assert update.cmd_status(argparse.Namespace(instance=str(inst), offline=True), rep) == 0
    assert not any("skipped by the last run" in line for line in rep.logs)


def test_update_parser_accepts_skip_errors_and_allow_missing():
    parser = argparse.ArgumentParser()
    update.add_parser(parser.add_subparsers(dest="command"))
    base = ["update", "--instance", "D:/GTS"]

    args = parser.parse_args([*base, "--skip-errors"])
    assert args.skip_errors is True and args.allow_missing is False
    assert parser.parse_args([*base, "--allow-missing"]).allow_missing is True
    plain = parser.parse_args(base)
    assert plain.skip_errors is False and plain.allow_missing is False
