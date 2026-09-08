"""Tests for `create._finish`'s summary output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collections2mo2 import create, ledger


class _CollectingReporter:
    def __init__(self):
        self.logs: list[str] = []
        self.warnings: list[str] = []

    def stage(self, name, total=None, *, stage_index=None, stage_count=None) -> None:
        return None

    def progress(self, done, total, label="", *, bytes_done=None, bytes_total=None) -> None:
        return None

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def done(self, name: str, summary: str = "") -> None:
        return None


def test_finish_prints_first_start_hint_with_mod_count(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    run.record("install", "ok", "42 mods installed")
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0, mod_count=42)
    assert rc == 0
    assert any("first start indexes 42 mods" in line for line in rep.logs)


def test_finish_omits_hint_when_mod_count_not_given(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0)
    assert rc == 0
    assert not any("first start indexes" in line for line in rep.logs)


def test_finish_omits_hint_when_run_failed(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    run.record("install", "failed", "boom")
    paths = create.Paths(tmp_path)

    rc = create._finish(run, rep, paths, started=0.0, mod_count=42)
    assert rc == 1
    assert not any("first start indexes" in line for line in rep.logs)


def test_install_tools_stage_runs_after_ledger_and_records_ok(monkeypatch, tmp_path: Path):
    from collections2mo2 import tools

    calls = []

    def fake_install(ns):
        calls.append(ns)
        print("  downloading xEdit")  # tools.py prints; must reach the reporter
        return 0

    monkeypatch.setattr(tools, "cmd_tools_install", fake_install)
    rep = _CollectingReporter()
    run = create.Run(rep)
    paths = create.Paths(tmp_path / "inst")

    rc = create.install_tools_stage(paths, ["xedit", "loot"], run, rep)

    assert rc == 0
    assert len(calls) == 1
    assert calls[0].ids == ["xedit", "loot"]
    assert Path(calls[0].mo2_dir) == paths.out
    assert calls[0].all_default is False and calls[0].force is False
    assert [(s.name, s.status) for s in run.stages] == [("tools", "ok")]
    assert not run.failed
    assert "  downloading xEdit" in rep.logs


def test_install_tools_stage_failure_is_a_failed_stage(monkeypatch, tmp_path: Path):
    from collections2mo2 import tools

    monkeypatch.setattr(tools, "cmd_tools_install", lambda ns: 1)
    rep = _CollectingReporter()
    run = create.Run(rep)

    rc = create.install_tools_stage(create.Paths(tmp_path / "inst"), ["xedit"], run, rep)

    assert rc == 1
    assert run.failed
    assert run.stages[0].name == "tools"
    assert any("tools failed" in w for w in rep.warnings)


# -- report_game_version: advisory, never a failed stage ---------------------------------


def test_report_game_version_logs_a_match(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.1170.0"
    )
    rep = _CollectingReporter()
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}

    result = create.report_game_version(manifest, "D:/Skyrim", "SkyrimSE", rep)

    assert result == ("match", "Game version 1.6.1170 matches the collection.")
    assert rep.logs == ["Game version 1.6.1170 matches the collection."]
    assert rep.warnings == []


def test_report_game_version_warns_on_a_mismatch(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.640.0"
    )
    rep = _CollectingReporter()
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}

    status, message = create.report_game_version(manifest, "D:/Skyrim", "SkyrimSE", rep)

    assert status == "mismatch"
    assert rep.logs == []
    assert rep.warnings == [message]
    assert "Skyrim Special Edition 1.6.1170" in message
    assert "the game at D:/Skyrim is 1.6.640" in message


def test_report_game_version_warns_when_the_version_cannot_be_read(monkeypatch):
    monkeypatch.setattr(create.game_version, "installed_game_version", lambda path, name: None)
    rep = _CollectingReporter()

    status, message = create.report_game_version(
        {"info": {"gameVersions": ["1.6.1170.0"]}}, "D:/Skyrim", "SkyrimSE", rep
    )

    assert status == "unknown"
    assert rep.warnings == [message]


def test_report_game_version_is_silent_without_a_manifest_version(monkeypatch):
    monkeypatch.setattr(
        create.game_version, "installed_game_version", lambda path, name: "1.6.1170.0"
    )
    rep = _CollectingReporter()

    assert create.report_game_version({"info": {}}, "D:/Skyrim", "SkyrimSE", rep) is None
    assert rep.logs == [] and rep.warnings == []


def test_report_game_version_addon_mismatch_is_a_note_not_a_warning(monkeypatch):
    monkeypatch.setattr(create.game_version, "installed_game_version", lambda *a, **k: "1.7.104.0")
    manifest = {"info": {"gameVersions": ["1.6.1170.0"]}}
    rep = _CollectingReporter()
    status, message = create.report_game_version(
        manifest, "D:/Skyrim", "SkyrimSE", rep, is_base=False
    )
    assert status == "mismatch"
    assert rep.warnings == []
    assert "add-on collection" in message
    assert rep.logs == [message]


# -- the archive store location (`create --downloads-dir`) -------------------------------


def test_paths_downloads_defaults_to_out_downloads(tmp_path: Path):
    assert create.Paths(tmp_path / "inst").downloads == tmp_path / "inst" / "downloads"


def test_paths_for_instance_without_a_ledger_uses_the_default(tmp_path: Path):
    inst = tmp_path / "inst"
    inst.mkdir()
    paths = create.Paths.for_instance(inst)
    assert paths.downloads_dir is None
    assert paths.downloads == paths.out / "downloads"


def test_paths_for_instance_picks_up_the_ledger_downloads_dir(tmp_path: Path):
    inst = tmp_path / "inst"
    inst.mkdir()
    custom = tmp_path / "archives"
    led = ledger.Ledger(inst)
    led.set_downloads_dir(custom)
    led.save()

    paths = create.Paths.for_instance(inst)
    assert paths.downloads == custom.resolve()
    # A layer shares the instance's archive store; it must follow it wherever it went.
    assert create.LayerPaths(paths, "h2uqa3", 68).downloads == custom.resolve()
    assert create.LayerPaths(paths, "h2uqa3", 68).downloads_json == paths.stage / (
        "h2uqa3-68.downloads.json"
    )


def test_downloads_path_warnings_flag_the_instance_folder_itself(tmp_path: Path):
    inst = tmp_path / "inst"
    warnings = create.downloads_path_warnings(inst, inst)
    assert any("the instance folder itself" in w for w in warnings)


def test_downloads_path_warnings_flag_a_folder_under_the_instances_mods(tmp_path: Path):
    inst = tmp_path / "inst"
    warnings = create.downloads_path_warnings(inst / "mods" / "archives", inst)
    assert any("MO2 would treat the archives as mod files" in w for w in warnings)


def test_downloads_path_warnings_flag_a_folder_inside_the_game_folder(tmp_path: Path):
    game = tmp_path / "Skyrim Special Edition"
    warnings = create.downloads_path_warnings(game / "archives", tmp_path / "inst", game)
    assert any("inside the game folder" in w for w in warnings)


def test_downloads_path_warnings_are_empty_for_a_sibling_folder(tmp_path: Path):
    inst = tmp_path / "inst"
    assert create.downloads_path_warnings(tmp_path / "archives", inst, tmp_path / "game") == []


def test_downloads_path_warnings_never_mention_the_path_length(tmp_path: Path):
    # Archives sit one level deep in the store, so the 260-character budget that makes
    # a long *instance* path fatal does not apply to the downloads folder.
    long_path = Path("D:/") / ("verylongfoldername" * 12)
    assert any("characters long" in w for w in create.instance_path_warnings(long_path))
    assert create.downloads_path_warnings(long_path, tmp_path / "inst") == []


def test_create_parser_accepts_downloads_dir():
    parser = argparse.ArgumentParser()
    create.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "create",
            "https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
            "--out",
            "D:/GTS",
            "--game-path",
            "D:/Games/Skyrim",
            "--downloads-dir",
            "E:/NexusDownloads",
        ]
    )
    assert args.downloads_dir == "E:/NexusDownloads"
    assert (
        parser.parse_args(["create", "u", "--out", "o", "--game-path", "g"]).downloads_dir is None
    )


def test_cmd_create_records_the_downloads_dir_and_hands_it_to_the_stages(
    monkeypatch, tmp_path: Path
):
    game = tmp_path / "game"
    game.mkdir()
    out = tmp_path / "inst"
    custom = tmp_path / "archives"
    seen: list[create.Paths] = []

    # `add_layer` is where download/inspect/install run; returning None makes cmd_create
    # stop right after the ledger has recorded the store, which is what we are pinning.
    monkeypatch.setattr(create, "add_layer", lambda paths, *a, **kw: seen.append(paths))
    monkeypatch.setattr(create, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("NEXUS_API_KEY", "test-key")

    rep = _CollectingReporter()
    rc = create.cmd_create(
        argparse.Namespace(
            url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
            out=str(out),
            game_path=str(game),
            downloads_dir=str(custom),
        ),
        reporter=rep,
    )

    assert rc == 0
    assert seen and seen[0].downloads == custom.resolve()
    assert custom.is_dir() and not (out / "downloads").exists()
    saved = json.loads((out / ledger.LEDGER_NAME).read_text(encoding="utf-8"))
    assert saved["downloads_dir"] == str(custom.resolve())


def test_cmd_create_leaves_the_ledger_alone_for_the_default_store(monkeypatch, tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()
    out = tmp_path / "inst"

    monkeypatch.setattr(create, "add_layer", lambda *a, **kw: None)
    monkeypatch.setattr(create, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("NEXUS_API_KEY", "test-key")

    rc = create.cmd_create(
        argparse.Namespace(
            url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
            out=str(out),
            game_path=str(game),
            downloads_dir=None,
        ),
        reporter=_CollectingReporter(),
    )

    assert rc == 0
    assert (out / "downloads").is_dir()
    # Nothing custom was asked for, so no ledger is written before the first layer.
    assert not (out / ledger.LEDGER_NAME).exists()


# -- what a failed stage leaves behind (`--allow-missing` / `--skip-errors`) -------------


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_inspect_failures_reads_the_failures_list(tmp_path: Path):
    path = _write_json(
        tmp_path / "inspect.json",
        {
            "entries": [{"tag": "ok", "name": "Fine Mod"}],
            "failures": [
                {"name": "Broken Mod", "file_name": "broken.7z", "error": "7za exit 2"},
                {"file_name": "nameless.rar", "error": ""},
                {},
            ],
        },
    )
    assert create._inspect_failures(path) == [
        ("Broken Mod", "7za exit 2"),
        ("nameless.rar", "could not be listed"),
        ("?", "could not be listed"),
    ]


def test_inspect_failures_of_a_clean_or_missing_file_is_empty(tmp_path: Path):
    assert create._inspect_failures(tmp_path / "nope.json") == []
    assert create._inspect_failures(_write_json(tmp_path / "i.json", {"entries": []})) == []


def test_install_failures_reads_the_failed_entries(tmp_path: Path):
    path = _write_json(
        tmp_path / "install.json",
        {
            "entries": [
                {"name": "Good Mod", "folder": "Good Mod", "strategy": "data"},
                {
                    "name": "Bad Mod",
                    "folder": "Bad Mod",
                    "strategy": "failed",
                    "warnings": [
                        "install failed: no Data folder",
                        "a plain note, not a failure",
                        "install failed: and nothing was written",
                    ],
                },
                {"folder": "Nameless", "strategy": "failed", "warnings": []},
            ]
        },
    )
    assert create._install_failures(path) == [
        ("Bad Mod", "install failed: no Data folder; install failed: and nothing was written"),
        ("Nameless", "install failed"),
    ]


def test_install_failures_of_a_clean_or_missing_file_is_empty(tmp_path: Path):
    assert create._install_failures(tmp_path / "nope.json") == []
    entries = {"entries": [{"name": "A", "strategy": "data"}]}
    assert create._install_failures(_write_json(tmp_path / "i.json", entries)) == []


def test_download_failures_splits_unavailable_from_mismatched(tmp_path: Path):
    path = _write_json(
        tmp_path / "downloads.json",
        {
            "entries": [
                {"name": "Fine", "status": "ok"},
                {"name": "Gone", "status": "error", "error": "404 Not Found"},
                {"file_name": "nameless.7z", "status": "error"},
                {"name": "Wrong File", "status": "md5_mismatch"},
            ]
        },
    )
    unavailable, mismatched = create._download_failures(path)
    assert unavailable == [("Gone", "404 Not Found"), ("nameless.7z", "download failed")]
    assert mismatched == ["Wrong File"]


# -- add_layer's per-stage gates ---------------------------------------------------------


class _FakeInfo:
    revision_number = 68
    name = "Test List"
    game = "skyrimspecialedition"


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def revision_info(self, ref, revision=None):
        return _FakeInfo()


def _layer_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
        revision=None,
        jobs=1,
        skip_survey=True,
        allow_missing=False,
        reuse_downloads=None,
        choices_overrides=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


_MANIFEST = {
    "info": {"name": "Test List", "domainName": "skyrimspecialedition"},
    "mods": [
        {"name": "Gone Mod", "source": {"tag": "t1", "md5": "m1"}},
        {"name": "Good Mod", "source": {"tag": "t2", "md5": "m2"}},
    ],
}


def _stub_stages(
    monkeypatch,
    *,
    downloads=None,
    download_rc=0,
    inspect=None,
    inspect_rc=0,
    install=None,
    install_rc=0,
):
    """Stub the fetch and the three stages that can fail; each writes its stage JSON."""

    def fake_fetch_manifest(client, ref, revision, collections_dir, info=None):
        path = Path(collections_dir) / ref.slug / "68" / "archive" / "collection.json"
        _write_json(path, _MANIFEST)
        return info or _FakeInfo(), path

    monkeypatch.setattr(create, "NexusClient", _FakeClient)
    monkeypatch.setattr(create, "fetch_manifest", fake_fetch_manifest)
    # Cosmetic Nexus-categories fetch; it has nothing to do with the failure gates.
    monkeypatch.setattr(create.categories, "prepare_layer", lambda *a, **kw: None)

    def fake_download(**kwargs):
        _write_json(Path(kwargs["json_path"]), downloads or {"entries": []})
        return download_rc

    def fake_inspect(ns, reporter=None):
        _write_json(Path(ns.out), inspect or {"entries": [{"tag": "t2"}]})
        return inspect_rc

    def fake_install(ns, reporter=None):
        _write_json(Path(ns.out), install or {"entries": []})
        return install_rc

    monkeypatch.setattr(create, "run_download", fake_download)
    monkeypatch.setattr(create.archive_inspect, "cmd_inspect", fake_inspect)
    monkeypatch.setattr(create.installer, "cmd_install", fake_install)


def _drive_add_layer(tmp_path: Path, args: argparse.Namespace):
    """Run `add_layer` against an empty instance; returns `(ctx, run, ledger, reporter)`."""
    paths = create.Paths(tmp_path / "inst")
    paths.stage.mkdir(parents=True, exist_ok=True)
    paths.mods.mkdir(parents=True, exist_ok=True)
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    rep = _CollectingReporter()
    run = create.Run(rep)
    led = ledger.Ledger(paths.out)
    ctx = create.add_layer(
        paths, args, led=led, api_key="test-key", game_path=game, run=run, rep=rep
    )
    return ctx, run, led, rep


def _stage(run: create.Run, name: str) -> create.StageResult:
    return next(s for s in run.stages if s.name == name)


_UNAVAILABLE = {"entries": [{"name": "Gone Mod", "status": "error", "error": "404 Not Found"}]}
_MISMATCHED = {
    "entries": [
        {"name": "Gone Mod", "status": "error", "error": "404 Not Found"},
        {"name": "Wrong File", "status": "md5_mismatch"},
    ]
}


def test_a_failed_download_stops_the_layer_without_a_flag(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, downloads=_UNAVAILABLE, download_rc=1)
    ctx, run, led, _ = _drive_add_layer(tmp_path, _layer_args())

    assert ctx is None
    assert _stage(run, "download").status == "failed"
    assert run.failed
    assert run.skipped == []
    assert led.data["layers"] == []


def test_allow_missing_carries_on_past_a_file_nexus_no_longer_serves(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, downloads=_UNAVAILABLE, download_rc=1)
    ctx, run, led, rep = _drive_add_layer(tmp_path, _layer_args(allow_missing=True))

    assert ctx is not None
    assert not run.failed
    download = _stage(run, "download")
    assert (download.status, download.detail) == ("warned", "1 archive(s) skipped")
    assert [(s.name, s.stage, s.reason) for s in run.skipped] == [
        ("Gone Mod", "download", "404 Not Found")
    ]
    assert ctx.skipped == run.skipped
    assert ctx.missing == ["Gone Mod"]
    assert any("--allow-missing" in w for w in rep.warnings)
    # The ledger remembers the list for `status` and the GUI.
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == [s.as_dict() for s in run.skipped]


def test_allow_missing_still_refuses_an_md5_mismatch(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, downloads=_MISMATCHED, download_rc=1)
    ctx, run, _, _ = _drive_add_layer(tmp_path, _layer_args(allow_missing=True))

    assert ctx is None
    assert _stage(run, "download").status == "failed"
    assert run.skipped == []


def test_skip_errors_tolerates_an_md5_mismatch(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, downloads=_MISMATCHED, download_rc=1)
    ctx, run, _, rep = _drive_add_layer(tmp_path, _layer_args(skip_errors=True))

    assert ctx is not None
    download = _stage(run, "download")
    assert (download.status, download.detail) == ("warned", "2 archive(s) skipped")
    assert [(s.name, s.reason) for s in run.skipped] == [
        ("Gone Mod", "404 Not Found"),
        ("Wrong File", create.MD5_MISMATCH_REASON),
    ]
    assert ctx.missing == ["Gone Mod", "Wrong File"]
    assert any("--skip-errors" in w for w in rep.warnings)


def test_a_failed_download_with_no_failed_entries_is_still_a_failure(monkeypatch, tmp_path: Path):
    # A non-zero exit the downloads.json cannot explain must never be shrugged off.
    _stub_stages(
        monkeypatch, downloads={"entries": [{"name": "Fine", "status": "ok"}]}, download_rc=1
    )
    ctx, run, _, _ = _drive_add_layer(tmp_path, _layer_args(skip_errors=True))

    assert ctx is None
    assert _stage(run, "download").status == "failed"


_INSPECT_FAILED = {
    "entries": [{"tag": "t2"}],
    "failures": [{"name": "Broken Mod", "error": "7za could not open the archive"}],
}


def test_a_failed_inspect_stops_the_layer_without_skip_errors(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, inspect=_INSPECT_FAILED, inspect_rc=1)
    # --allow-missing is a download-stage flag only; it must not cover a bad archive.
    ctx, run, _, _ = _drive_add_layer(tmp_path, _layer_args(allow_missing=True))

    assert ctx is None
    assert _stage(run, "inspect").status == "failed"


def test_skip_errors_carries_on_past_an_archive_that_cannot_be_listed(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, inspect=_INSPECT_FAILED, inspect_rc=1)
    ctx, run, led, rep = _drive_add_layer(tmp_path, _layer_args(skip_errors=True))

    assert ctx is not None
    inspect = _stage(run, "inspect")
    assert (inspect.status, inspect.detail) == ("warned", "1 archive(s) skipped")
    assert [(s.name, s.stage, s.reason) for s in ctx.skipped] == [
        ("Broken Mod", "inspect", "7za could not open the archive")
    ]
    assert ctx.missing == []  # `missing` is the download stage's view only
    assert any("could not be listed" in w for w in rep.warnings)
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == [s.as_dict() for s in ctx.skipped]


_INSTALL_FAILED = {
    "entries": [
        {"name": "Good Mod", "folder": "Good Mod", "strategy": "data", "md5": "m2", "tag": "t2"},
        {
            "name": "Bad Mod",
            "folder": "Bad Mod",
            "strategy": "failed",
            "md5": "m1",
            "tag": "t1",
            "warnings": ["install failed: nothing extracted"],
        },
    ]
}


def test_a_failed_install_stops_the_layer_without_skip_errors(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, install=_INSTALL_FAILED, install_rc=1)
    ctx, run, led, _ = _drive_add_layer(tmp_path, _layer_args(allow_missing=True))

    assert ctx is None
    assert _stage(run, "install").status == "failed"
    assert led.data["mods"] == {}


def test_skip_errors_leaves_a_failed_install_out_of_the_ledger(monkeypatch, tmp_path: Path):
    _stub_stages(monkeypatch, install=_INSTALL_FAILED, install_rc=1)
    ctx, run, led, rep = _drive_add_layer(tmp_path, _layer_args(skip_errors=True))

    assert ctx is not None
    install = _stage(run, "install")
    assert (install.status, install.detail) == ("warned", "1 mod(s) skipped")
    assert [(s.name, s.stage, s.reason) for s in ctx.skipped] == [
        ("Bad Mod", "install", "install failed: nothing extracted")
    ]
    # The entry stays in install.json for a later retry, but owns no mods/ folder.
    assert list(led.data["mods"]) == ["Good Mod"]
    assert ctx.installed == 1
    assert any("could not be installed" in w for w in rep.warnings)
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == [s.as_dict() for s in ctx.skipped]


def test_a_clean_layer_records_no_skipped_mods(monkeypatch, tmp_path: Path):
    _stub_stages(
        monkeypatch,
        install={"entries": [{"name": "Good Mod", "folder": "Good Mod", "strategy": "data"}]},
    )
    ctx, run, led, _ = _drive_add_layer(tmp_path, _layer_args(skip_errors=True))

    assert ctx is not None and ctx.skipped == [] and run.skipped == []
    assert [s.status for s in run.stages if s.name == "download"] == ["ok"]
    assert led.layer_skipped(led.layer("h2uqa3", 68)) == []


# -- the summary and the flag itself -----------------------------------------------------


def test_finish_lists_the_skipped_mods_and_still_succeeds(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    run.record("install", "warned", "1 mod(s) skipped")
    run.skip("Gone Mod", "download", "404 Not Found")

    rc = create._finish(run, rep, create.Paths(tmp_path), started=0.0)

    assert rc == 0
    assert not run.failed
    assert any("Gone Mod  [download] 404 Not Found" in w for w in rep.warnings)
    assert any("1 mod(s) are NOT in the instance" in w for w in rep.warnings)


def test_finish_says_nothing_about_skips_for_a_clean_run(tmp_path: Path):
    rep = _CollectingReporter()
    run = create.Run(reporter=rep)
    create._finish(run, rep, create.Paths(tmp_path), started=0.0)
    assert not any("NOT in the instance" in w for w in rep.warnings)


def test_skip_errors_requested_reads_the_flag_and_defaults_to_false():
    assert create.skip_errors_requested(argparse.Namespace(skip_errors=True)) is True
    assert create.skip_errors_requested(argparse.Namespace(skip_errors=False)) is False
    # The GUI and older callers build a Namespace without the attribute at all.
    assert create.skip_errors_requested(argparse.Namespace()) is False


def test_create_parser_accepts_skip_errors_and_allow_missing():
    parser = argparse.ArgumentParser()
    create.add_parser(parser.add_subparsers(dest="command"))
    base = ["create", "url", "--out", "D:/GTS", "--game-path", "D:/Games/Skyrim"]

    args = parser.parse_args([*base, "--skip-errors"])
    assert args.skip_errors is True and args.allow_missing is False
    plain = parser.parse_args(base)
    assert plain.skip_errors is False and plain.allow_missing is False


def _downloads_json_with(lp: create.LayerPaths, manifest: Path, rows: list[dict]) -> None:
    lp.downloads_json.parent.mkdir(parents=True, exist_ok=True)
    lp.downloads_json.write_text(json.dumps({"manifest": str(manifest), "entries": rows}))


def test_downloads_are_current_reruns_for_a_bundle_recorded_as_unsupported(tmp_path: Path):
    """A downloads.json from before bundles were handled marks the bundle mod `unsupported`;
    that row must send the stage back to work instead of being skipped over."""
    paths = create.Paths(tmp_path / "inst")
    lp = create.LayerPaths(paths, "xa2h3u", 33)
    manifest = tmp_path / "collection.json"
    manifest.write_text("{}")
    archive = paths.downloads / "a.7z"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"x")
    ok_row = {"status": "ok", "path": str(archive), "source_type": "nexus"}

    _downloads_json_with(lp, manifest, [ok_row, {"status": "unsupported", "source_type": "browse"}])
    assert create.downloads_are_current(lp, manifest, 2) is True

    _downloads_json_with(lp, manifest, [ok_row, {"status": "unsupported", "source_type": "bundle"}])
    assert create.downloads_are_current(lp, manifest, 2) is False
