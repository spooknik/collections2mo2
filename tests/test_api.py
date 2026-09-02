"""Tests for `api.py`, the facade the GUI calls into the engine through.

No network access: engine entry points (`create.cmd_create`, `layers.cmd_add`,
`layers.cmd_remove`, `tools.cmd_tools_install`) are monkeypatched to capture the
arguments `api.py` built for them, and Nexus HTTP calls are faked.
"""

from __future__ import annotations

import argparse
import os

import pytest

from collections2wabbajack import api
from collections2wabbajack.reporter import NullReporter

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
    assert captured["reporter"] is reporter


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
    from collections2wabbajack import update as update_mod

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
    from collections2wabbajack import wabbajack as wabbajack_mod

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
