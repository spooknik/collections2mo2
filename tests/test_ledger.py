"""Tests for the ledger's record of where an instance keeps its archives.

`create --downloads-dir` lets the archive store live anywhere; the ledger is the only
thing that remembers it, and every later command (`add`, `update`, `tools`, `build`,
the Wabbajack compile) reads it back through `Ledger.downloads_dir` or the module-level
`downloads_dir()`. These tests pin both, including the fallbacks for an instance that
has no ledger, one written before the key existed, and one whose ledger is unreadable.
"""

from __future__ import annotations

import json
from pathlib import Path

from collections2mo2 import ledger


def _instance(tmp_path: Path) -> Path:
    inst = tmp_path / "inst"
    inst.mkdir()
    return inst


# -- Ledger.downloads_dir / set_downloads_dir ----------------------------------------


def test_a_fresh_ledger_has_a_null_downloads_dir_and_the_default_location(tmp_path: Path):
    inst = _instance(tmp_path)
    led = ledger.Ledger(inst)
    assert led.data["downloads_dir"] is None
    assert led.downloads_dir == inst / ledger.DEFAULT_DOWNLOADS_NAME


def test_set_downloads_dir_round_trips_a_custom_folder_through_save_and_load(tmp_path: Path):
    inst = _instance(tmp_path)
    custom = tmp_path / "archives"
    led = ledger.Ledger(inst)
    led.set_downloads_dir(custom)
    led.save()

    assert json.loads(led.path.read_text(encoding="utf-8"))["downloads_dir"] == str(
        custom.resolve()
    )
    assert ledger.load(inst).downloads_dir == custom.resolve()


def test_set_downloads_dir_stores_null_for_the_default_location(tmp_path: Path):
    inst = _instance(tmp_path)
    led = ledger.Ledger(inst)
    led.set_downloads_dir(inst / ledger.DEFAULT_DOWNLOADS_NAME)
    led.save()

    # An instance that never asked for a custom store must read exactly like one
    # written before the key existed.
    assert json.loads(led.path.read_text(encoding="utf-8"))["downloads_dir"] is None
    assert ledger.load(inst).downloads_dir == inst / ledger.DEFAULT_DOWNLOADS_NAME


def test_set_downloads_dir_none_clears_the_record(tmp_path: Path):
    inst = _instance(tmp_path)
    led = ledger.Ledger(inst)
    led.set_downloads_dir(tmp_path / "archives")
    led.set_downloads_dir(None)
    assert led.data["downloads_dir"] is None
    assert led.downloads_dir == inst / ledger.DEFAULT_DOWNLOADS_NAME


# -- resolve_downloads_dir ------------------------------------------------------------


def test_resolve_downloads_dir_falls_back_to_the_default_for_an_empty_value(tmp_path: Path):
    inst = _instance(tmp_path)
    assert ledger.resolve_downloads_dir(inst, None) == inst / "downloads"
    assert ledger.resolve_downloads_dir(inst, "") == inst / "downloads"


def test_resolve_downloads_dir_resolves_a_relative_value_against_the_instance(tmp_path: Path):
    inst = _instance(tmp_path)
    assert ledger.resolve_downloads_dir(inst, "archives") == inst / "archives"


# -- the module-level downloads_dir() -------------------------------------------------


def test_downloads_dir_without_a_ledger_is_the_instance_default(tmp_path: Path):
    inst = _instance(tmp_path)
    assert ledger.downloads_dir(inst) == inst / "downloads"


def test_downloads_dir_of_a_default_ledger_is_the_instance_default(tmp_path: Path):
    inst = _instance(tmp_path)
    ledger.Ledger(inst).save()
    assert ledger.downloads_dir(inst) == inst / "downloads"


def test_downloads_dir_reads_a_custom_ledger(tmp_path: Path):
    inst = _instance(tmp_path)
    custom = tmp_path / "archives"
    led = ledger.Ledger(inst)
    led.set_downloads_dir(custom)
    led.save()
    assert ledger.downloads_dir(inst) == custom.resolve()


def test_downloads_dir_resolves_a_relative_ledger_value_against_the_instance(tmp_path: Path):
    inst = _instance(tmp_path)
    # A hand-edited ledger may carry a relative path; it belongs to the instance.
    (inst / ledger.LEDGER_NAME).write_text(json.dumps({"downloads_dir": "archives"}), "utf-8")
    assert ledger.downloads_dir(inst) == inst / "archives"


def test_downloads_dir_reads_the_pre_rename_ledger_name(tmp_path: Path):
    inst = _instance(tmp_path)
    custom = tmp_path / "archives"
    (inst / ledger.LEGACY_LEDGER_NAME).write_text(
        json.dumps({"downloads_dir": str(custom)}), "utf-8"
    )
    assert ledger.downloads_dir(inst) == custom


def test_downloads_dir_falls_back_when_the_ledger_is_garbled(tmp_path: Path):
    inst = _instance(tmp_path)
    (inst / ledger.LEDGER_NAME).write_text("{ this is not json", encoding="utf-8")
    assert ledger.downloads_dir(inst) == inst / "downloads"


def test_downloads_dir_falls_back_when_the_ledger_is_not_an_object(tmp_path: Path):
    inst = _instance(tmp_path)
    (inst / ledger.LEDGER_NAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert ledger.downloads_dir(inst) == inst / "downloads"


# -- the mods a run went in without (`--skip-errors` / `--allow-missing`) -------------


def test_set_layer_skipped_round_trips_through_save_and_load(tmp_path: Path):
    inst = _instance(tmp_path)
    led = ledger.Ledger(inst)
    led.register_layer("h2uqa3", 68, name="GTS")
    led.set_layer_skipped(
        "h2uqa3",
        68,
        [{"name": "Gone Mod", "stage": "download", "reason": "404 Not Found"}],
    )
    led.save()

    reloaded = ledger.load(inst)
    layer = reloaded.layer("h2uqa3", 68)
    assert layer is not None
    assert reloaded.layer_skipped(layer) == [
        {"name": "Gone Mod", "stage": "download", "reason": "404 Not Found"}
    ]


def test_set_layer_skipped_replaces_an_earlier_runs_list(tmp_path: Path):
    inst = _instance(tmp_path)
    led = ledger.Ledger(inst)
    led.register_layer("h2uqa3", 68)
    led.set_layer_skipped("h2uqa3", 68, [{"name": "A", "stage": "install", "reason": "boom"}])
    # A clean re-run must clear the list rather than append to it.
    led.set_layer_skipped("h2uqa3", 68, [])
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == []


def test_set_layer_skipped_is_a_no_op_for_an_unknown_layer(tmp_path: Path):
    led = ledger.Ledger(_instance(tmp_path))
    led.register_layer("h2uqa3", 68)
    led.set_layer_skipped("nope", 1, [{"name": "A", "stage": "install", "reason": "boom"}])
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == []


def test_layer_skipped_of_a_layer_without_the_key_is_empty(tmp_path: Path):
    led = ledger.Ledger(_instance(tmp_path))
    layer = led.register_layer("h2uqa3", 68)
    assert "skipped" not in layer
    assert led.layer_skipped(layer) == []


def test_layer_skipped_ignores_non_dict_junk(tmp_path: Path):
    # A hand-edited ledger may hold anything; only `{name, stage, reason}` objects count.
    led = ledger.Ledger(_instance(tmp_path))
    layer = led.register_layer("h2uqa3", 68)
    layer["skipped"] = ["Gone Mod", None, 7, {"name": "Real", "stage": "inspect", "reason": "bad"}]
    assert led.layer_skipped(layer) == [{"name": "Real", "stage": "inspect", "reason": "bad"}]


def test_layer_skipped_returns_copies_not_the_stored_dicts(tmp_path: Path):
    led = ledger.Ledger(_instance(tmp_path))
    led.register_layer("h2uqa3", 68)
    led.set_layer_skipped("h2uqa3", 68, [{"name": "A", "stage": "download", "reason": "404"}])
    got = led.layer_skipped(led.layer("h2uqa3", 68))
    got[0]["name"] = "mutated"
    assert led.layer_skipped(led.layer("h2uqa3", 68))[0]["name"] == "A"
