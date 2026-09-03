"""Tests for `api.py`, the facade the GUI calls into the engine through.

No network access: engine entry points (`create.cmd_create`, `layers.cmd_add`,
`layers.cmd_remove`, `tools.cmd_tools_install`) are monkeypatched to capture the
arguments `api.py` built for them, and Nexus HTTP calls are faked.
"""

from __future__ import annotations

import argparse
import os
import stat

import pytest

from collections2mo2 import api
from collections2mo2.reporter import NullReporter

# -- create_instance: argument mapping ------------------------------------------------


def test_create_instance_maps_arguments(monkeypatch):
    captured = {}

    def fake_cmd_create(ns: argparse.Namespace, reporter=None):
        captured["ns"] = ns
        captured["reporter"] = reporter
        return 0

    monkeypatch.setattr(api.create, "cmd_create", fake_cmd_create)

    reporter = NullReporter()
    rc = api.create_instance(
        url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
        out="D:/Skyrim",
        game_path="E:/Games/Skyrim Special Edition",
        revision=68,
        stock_game=True,
        jobs=8,
        resolution="1920x1080",
        vsync="on",
        window="borderless",
        skip_survey=True,
        allow_missing=True,
        reporter=reporter,
    )

    assert rc == 0
    ns = captured["ns"]
    assert ns.url.endswith("/collections/h2uqa3")
    assert ns.out == "D:/Skyrim"
    assert ns.game_path == "E:/Games/Skyrim Special Edition"
    assert ns.revision == 68
    assert ns.stock_game is True
    assert ns.jobs == 8
    assert ns.resolution == "1920x1080"
    assert ns.vsync == "on"
    assert ns.window == "borderless"
    assert ns.skip_survey is True
    assert ns.allow_missing is True
    assert ns.mo2_version == api.build.DEFAULT_MO2_VERSION
    assert ns.rootbuilder_version == api.build.DEFAULT_ROOTBUILDER_VERSION
    assert ns.tools == []
    assert captured["reporter"] is reporter


def test_create_instance_passes_tool_ids_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        api.create, "cmd_create", lambda ns, reporter=None: captured.setdefault("ns", ns) and 0
    )
    rc = api.create_instance(
        url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
        out="D:/Skyrim",
        game_path="E:/Games/Skyrim Special Edition",
        tool_ids=["xedit", "loot"],
    )
    assert rc == 0
    assert captured["ns"].tools == ["xedit", "loot"]


def test_create_instance_validates_resolution(monkeypatch):
    monkeypatch.setattr(api.create, "cmd_create", lambda ns, reporter=None: 0)
    with pytest.raises(argparse.ArgumentTypeError):
        api.create_instance(
            url="https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3",
            out="D:/Skyrim",
            game_path="E:/Games/Skyrim",
            resolution="not-a-resolution",
        )


# -- add / remove layer: argument mapping ---------------------------------------------


def test_add_collection_layer_maps_arguments(monkeypatch):
    captured = {}

    def fake_cmd_add(ns, reporter=None):
        captured["ns"] = ns
        return 0

    monkeypatch.setattr(api.layers, "cmd_add", fake_cmd_add)
    rc = api.add_collection_layer(
        instance_dir="D:/Skyrim", url="https://example/collections/xk05aw", revision=3, jobs=2
    )
    assert rc == 0
    ns = captured["ns"]
    assert ns.instance == "D:/Skyrim"
    assert ns.url.endswith("xk05aw")
    assert ns.revision == 3
    assert ns.jobs == 2


def test_remove_collection_layer_maps_arguments(monkeypatch):
    captured = {}

    def fake_cmd_remove(ns, reporter=None):
        captured["ns"] = ns
        return 0

    monkeypatch.setattr(api.layers, "cmd_remove", fake_cmd_remove)
    rc = api.remove_collection_layer(instance_dir="D:/Skyrim", slug="xk05aw", purge_downloads=True)
    assert rc == 0
    ns = captured["ns"]
    assert ns.slug == "xk05aw"
    assert ns.instance == "D:/Skyrim"
    assert ns.purge_downloads is True
    assert ns.force is False


# -- tools ------------------------------------------------------------------------------


def test_install_tools_maps_arguments_and_redirects_output(monkeypatch, tmp_path):
    captured = {}

    def fake_cmd_tools_install(ns):
        captured["ns"] = ns
        print("hello from tools.py")
        return 0

    monkeypatch.setattr(api.tools, "cmd_tools_install", fake_cmd_tools_install)

    logged = []

    class _Rep(NullReporter):
        def log(self, msg):
            logged.append(msg)

    ok = api.install_tools(tmp_path, ["xedit"], force=True, reporter=_Rep())
    assert ok is True
    ns = captured["ns"]
    assert ns.ids == ["xedit"]
    assert ns.mo2_dir == str(tmp_path)
    assert ns.force is True
    assert "hello from tools.py" in logged


def test_list_tool_groups_has_essential_and_installable_dyndolod():
    groups = dict(api.list_tool_groups())
    assert "essential" in groups
    ids = {e.id for entries in groups.values() for e in entries}
    assert "xedit" in ids
    dyndolod = next(e for entries in groups.values() for e in entries if e.id == "dyndolod")
    assert dyndolod.disabled is False
    assert dyndolod.status == "not installed"


# -- misc GUI-only helpers ---------------------------------------------------------------


def test_path_warnings_flags_long_paths():
    long_path = "C:/" + "a" * 60
    warnings = api.path_warnings(long_path)
    assert any("characters" in w for w in warnings)


def test_path_warnings_empty_for_short_path():
    assert api.path_warnings("D:/Skyrim") == []


def _neutral_location_env(monkeypatch, tmp_path):
    """Make the environment-derived warnings deterministic: a home and the Program
    Files / Windows variables all pointed somewhere the test path is not under."""
    home = tmp_path / "home"
    monkeypatch.setattr(api.create.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", "C:\\Program Files (x86)")
    monkeypatch.setenv("ProgramW6432", "C:\\Program Files")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.delenv("OneDrive", raising=False)
    return home


def test_path_warnings_flags_program_files(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings("C:/Program Files/Skyrim")
    assert any("Program Files" in w and "UAC" in w for w in warnings)


def test_path_warnings_flags_program_files_x86(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings("C:/Program Files (x86)/GTS")
    assert any("Program Files" in w for w in warnings)


def test_path_warnings_flags_the_windows_folder(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings("C:/Windows/GTS")
    assert any("Windows folder" in w for w in warnings)


def test_path_warnings_flags_the_desktop(monkeypatch, tmp_path):
    home = _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings(home / "Desktop" / "GTS")
    assert any("Desktop" in w for w in warnings)


def test_path_warnings_flags_the_onedrive_desktop(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    warnings = api.path_warnings(tmp_path / "OneDrive" / "Desktop" / "GTS")
    assert any("Desktop" in w for w in warnings)


def test_path_warnings_flags_a_steam_library(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings("D:/SteamLibrary/steamapps/common/GTS")
    assert any("Steam library" in w for w in warnings)


def test_path_warnings_flags_the_game_folder(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    warnings = api.path_warnings("D:/Skyrim/GTS", "D:/Skyrim")
    assert any("inside the game folder" in w for w in warnings)
    # ...and the same path is fine when it is not under the game folder.
    assert not any("inside the game folder" in w for w in api.path_warnings("D:/GTS", "D:/Skyrim"))


def test_path_warnings_stay_quiet_for_a_plain_folder(monkeypatch, tmp_path):
    _neutral_location_env(monkeypatch, tmp_path)
    assert api.path_warnings("D:/GTS", "D:/Skyrim") == []


# -- game version check ------------------------------------------------------------------


def test_game_version_check_delegates_and_reports_a_mismatch(monkeypatch):
    monkeypatch.setattr(api.game_version, "installed_game_version", lambda p, n: "1.6.640.0")
    result = api.game_version_check(["1.6.1170.0"], "D:/Skyrim", "skyrimspecialedition")
    assert result is not None
    assert result[0] == "mismatch"
    assert "Skyrim Special Edition 1.6.1170" in result[1]


def test_game_version_check_returns_none_without_a_target_version(monkeypatch):
    monkeypatch.setattr(api.game_version, "installed_game_version", lambda p, n: "1.6.640.0")
    assert api.game_version_check([], "D:/Skyrim") is None


def test_short_game_version():
    assert api.short_game_version("1.6.1170.0") == "1.6.1170"


class _FakeGraphQLClient:
    """A `NexusClient` stand-in whose `graphql` returns one canned payload."""

    def __init__(self, data):
        self._data = data
        self.queries: list[str] = []

    def graphql(self, query, variables=None):
        self.queries.append(query)
        return self._data


_SUMMARY_PAYLOAD = {
    "collection": {
        "name": "Gate to Sovngarde",
        "summary": "A collection",
        "game": {"domainName": "skyrimspecialedition"},
        "user": {"name": "Curator"},
        "latestPublishedRevision": {"revisionNumber": 118},
        "revisions": [{"revisionNumber": 117, "status": "published"}],
    },
    "collectionRevision": {
        "revisionNumber": 117,
        "modCount": 1500,
        "totalSize": 123,
        "downloadLink": "/v2/x",
        "gameVersions": [{"reference": "1.7.104.0"}],
    },
}


def test_fetch_collection_summary_reads_game_versions(monkeypatch):
    client = _FakeGraphQLClient(_SUMMARY_PAYLOAD)
    monkeypatch.setattr(api, "NexusClient", lambda api_key=None: client)
    summary = api.fetch_collection_summary(
        "https://www.nexusmods.com/games/skyrimspecialedition/collections/qdurkx"
    )
    assert summary.game_versions == ["1.7.104.0"]
    assert "gameVersions" in client.queries[0]


def test_fetch_collection_summary_without_game_versions(monkeypatch):
    payload = {
        "collection": _SUMMARY_PAYLOAD["collection"],
        "collectionRevision": {
            k: v for k, v in _SUMMARY_PAYLOAD["collectionRevision"].items() if k != "gameVersions"
        },
    }
    monkeypatch.setattr(api, "NexusClient", lambda api_key=None: _FakeGraphQLClient(payload))
    summary = api.fetch_collection_summary(
        "https://www.nexusmods.com/games/skyrimspecialedition/collections/qdurkx"
    )
    assert summary.game_versions == []


def test_default_instance_dir_sanitizes_name():
    result = api.default_instance_dir('Bad<>:"/\\|?*Name')
    assert result.name == "BadName"


def test_format_bytes():
    assert api.format_bytes(0) == "0 B"
    assert "MB" in api.format_bytes(5_000_000)
    assert "GB" in api.format_bytes(5_000_000_000)


def test_has_update_and_wabbajack_support_are_bool():
    # update.py / wabbajack.py may or may not exist yet (developed concurrently);
    # this must never raise, whichever is true.
    assert isinstance(api.has_update_support(), bool)
    assert isinstance(api.has_wabbajack_support(), bool)


@pytest.mark.skipif(not api.has_update_support(), reason="update.py not present in this build")
def test_update_collection_layer_maps_arguments(monkeypatch):
    from collections2mo2 import update as update_mod

    captured = {}

    def fake_cmd_update(ns, reporter=None):
        captured["ns"] = ns
        return 0

    monkeypatch.setattr(update_mod, "cmd_update", fake_cmd_update)
    rc = api.update_collection_layer(instance_dir="D:/Skyrim", slug="h2uqa3", to="latest")
    assert rc == 0
    ns = captured["ns"]
    assert ns.instance == "D:/Skyrim"
    assert ns.layer == "h2uqa3"
    assert ns.to == "latest"
    assert ns.yes is True  # the GUI has no terminal to confirm on -- must always be True
    assert ns.dry_run is False


@pytest.mark.skipif(
    not api.has_wabbajack_support(), reason="wabbajack.py not present in this build"
)
def test_export_to_wabbajack_maps_arguments(monkeypatch):
    from collections2mo2 import wabbajack as wabbajack_mod

    captured = {}

    def fake_cmd_wabbajack(ns, reporter=None):
        captured["ns"] = ns
        return 0

    monkeypatch.setattr(wabbajack_mod, "cmd_wabbajack", fake_cmd_wabbajack)
    rc = api.export_to_wabbajack("D:/Skyrim", name="My List")
    assert rc == 0
    ns = captured["ns"]
    assert ns.instance == "D:/Skyrim"
    assert ns.name == "My List"
    assert ns.dry_run is False


# -- sign-in: keyring + validate_api_key, both with fakes --------------------------------


class _FakeKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        del self.store[(service, username)]


def test_api_key_keyring_roundtrip(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(api.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(api.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(api.keyring, "delete_password", fake.delete_password)

    assert api.get_saved_api_key() is None
    api.save_api_key("abc123")
    assert api.get_saved_api_key() == "abc123"
    api.clear_api_key()
    assert api.get_saved_api_key() is None


def test_activate_api_key_sets_env(monkeypatch):
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    api.activate_api_key("my-key")
    assert os.environ["NEXUS_API_KEY"] == "my-key"


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, url, timeout=None):
        return self._response


class _FakeClient:
    def __init__(self, session):
        self.session = session


def test_validate_api_key_success(monkeypatch):
    response = _FakeResponse(200, {"name": "Spooknik", "is_premium": True})
    monkeypatch.setattr(
        api, "NexusClient", lambda api_key=None: _FakeClient(_FakeSession(response))
    )
    result = api.validate_api_key("some-key")
    assert result.name == "Spooknik"
    assert result.is_premium is True


def test_validate_api_key_rejected(monkeypatch):
    response = _FakeResponse(401, {})
    monkeypatch.setattr(
        api, "NexusClient", lambda api_key=None: _FakeClient(_FakeSession(response))
    )
    with pytest.raises(api.ApiError):
        api.validate_api_key("bad-key")


def test_validate_api_key_requires_nonempty():
    with pytest.raises(api.ApiError):
        api.validate_api_key("   ")


# -- pre-rename data dir / env var -------------------------------------------------------


def test_data_dir_override_accepts_the_legacy_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("C2MO2_DATA_DIR", raising=False)
    monkeypatch.setenv("C2WJ_DATA_DIR", str(tmp_path / "legacy-data"))
    monkeypatch.setattr(api.sevenzip_mod, "TOOLS_DIR", api.sevenzip_mod.TOOLS_DIR)
    monkeypatch.setattr(api.build, "CACHE_DIR", api.build.CACHE_DIR)
    monkeypatch.setattr(api.tools_mod, "CACHE_DIR", api.tools_mod.CACHE_DIR)

    base = api._apply_data_dir_override()
    assert base == tmp_path / "legacy-data"
    assert api.sevenzip_mod.TOOLS_DIR == tmp_path / "legacy-data" / "tools"
    assert api.build.CACHE_DIR == tmp_path / "legacy-data" / "tools" / "cache"


def test_data_dir_override_prefers_the_current_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("C2MO2_DATA_DIR", str(tmp_path / "current"))
    monkeypatch.setenv("C2WJ_DATA_DIR", str(tmp_path / "legacy"))
    monkeypatch.setattr(api.sevenzip_mod, "TOOLS_DIR", api.sevenzip_mod.TOOLS_DIR)
    monkeypatch.setattr(api.build, "CACHE_DIR", api.build.CACHE_DIR)
    monkeypatch.setattr(api.tools_mod, "CACHE_DIR", api.tools_mod.CACHE_DIR)

    assert api._apply_data_dir_override() == tmp_path / "current"


def test_data_dir_override_is_none_without_either_env_var(monkeypatch):
    monkeypatch.delenv("C2MO2_DATA_DIR", raising=False)
    monkeypatch.delenv("C2WJ_DATA_DIR", raising=False)
    monkeypatch.setattr(api.sys, "frozen", False, raising=False)
    assert api._apply_data_dir_override() is None


def test_default_data_dir_reuses_the_legacy_folder_when_it_is_the_only_one(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / api.LEGACY_DATA_DIR_NAME).mkdir()
    assert api._default_data_dir() == tmp_path / api.LEGACY_DATA_DIR_NAME

    (tmp_path / "collections2mo2").mkdir()
    assert api._default_data_dir() == tmp_path / "collections2mo2"


def test_default_data_dir_is_the_new_name_on_a_clean_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert api._default_data_dir() == tmp_path / "collections2mo2"


def test_saved_api_key_falls_back_to_the_legacy_keyring_service(monkeypatch):
    fake = _FakeKeyring()
    fake.store[(api.LEGACY_KEYRING_SERVICE, api.KEYRING_USERNAME)] = "old-key"
    monkeypatch.setattr(api.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(api.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(api.keyring, "delete_password", fake.delete_password)

    assert api.get_saved_api_key() == "old-key"

    # A key saved under the current name wins, and clearing removes both.
    api.save_api_key("new-key")
    assert fake.store[(api.KEYRING_SERVICE, api.KEYRING_USERNAME)] == "new-key"
    assert api.get_saved_api_key() == "new-key"
    api.clear_api_key()
    assert api.get_saved_api_key() is None
    assert fake.store == {}


# -- delete_instance ---------------------------------------------------------------------


def test_delete_instance_refuses_a_folder_without_a_ledger(tmp_path):
    """The only guard between a mistyped path and `shutil.rmtree`: no ledger, no delete."""
    folder = tmp_path / "not-an-instance"
    (folder / "important").mkdir(parents=True)
    (folder / "important" / "data.txt").write_text("keep me")

    with pytest.raises(api.ApiError) as excinfo:
        api.delete_instance(folder)

    assert "not a c2mo2 instance" in str(excinfo.value)
    assert (folder / "important" / "data.txt").is_file()


def test_delete_instance_removes_the_folder_including_read_only_files(tmp_path):
    instance = tmp_path / "instance"
    (instance / "mods" / "Some Mod").mkdir(parents=True)
    (instance / "c2mo2-instance.json").write_text("{}")
    read_only = instance / "mods" / "Some Mod" / "meta.ini"
    read_only.write_text("[General]")
    os.chmod(read_only, stat.S_IREAD)

    api.delete_instance(instance, reporter=NullReporter())

    assert not instance.exists()


def test_delete_instance_locked_file_message_names_mo2(tmp_path, monkeypatch):
    """MO2 still open on the instance: say which file, and what to do about it."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "c2mo2-instance.json").write_text("{}")
    locked = instance / "ModOrganizer.exe"
    locked.write_text("x")

    def fake_rmtree(path, **kwargs):
        raise PermissionError(13, "The process cannot access the file", str(locked))

    monkeypatch.setattr(api.shutil, "rmtree", fake_rmtree)

    with pytest.raises(api.ApiError) as excinfo:
        api.delete_instance(instance)

    message = str(excinfo.value)
    assert "Mod Organizer 2" in message
    assert "ModOrganizer.exe" in message
    assert "could not be fully removed" in message
