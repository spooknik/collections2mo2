"""Tests for tools.py: catalogue schema, companion-mod install/remove, ledger wiring.

No network: `resolve_source` and `_download_cached` are monkeypatched so a companion
mod "install" just extracts a small local test archive built with zipfile. Extraction
itself goes through sevenzip.extract, which needs tools/7za.exe -- those tests are
`local` and skip themselves when it is not bootstrapped, same convention as
test_archive_inspect.py / test_build.py's archive-listing tests.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pytest

from collections2wabbajack import ledger as ledger_mod
from collections2wabbajack import tools
from collections2wabbajack.sevenzip import TOOLS_DIR

_SEVENZIP_AVAILABLE = (TOOLS_DIR / "7za.exe").exists() and (TOOLS_DIR / "7z.dll").exists()


# -- catalogue schema ------------------------------------------------------------------


def test_dyndolod_is_installable_with_companion_mods():
    catalog = tools.load_catalog()
    entry = next(e for e in catalog if e["id"] == "dyndolod")
    assert not (entry.get("install") or {}).get("disabled")
    assert entry["source"]["type"] == "nexus"
    assert entry["source"]["mod_id"] == 68518
    assert entry["companion_mods"] == ["dyndolod-resources", "dyndolod-dll-ng"]
    binaries = {e["binary"] for e in entry["executables"]}
    assert "DynDOLODx64.exe" in binaries
    assert "TexGenx64.exe" in binaries
    assert all(e["workingDirectory"] == "tool" for e in entry["executables"])


def test_companion_catalog_has_the_two_dyndolod_mods():
    companions = tools.load_companion_catalog()
    assert {c["id"] for c in companions} == {"dyndolod-resources", "dyndolod-dll-ng"}
    by_id = {c["id"]: c for c in companions}
    assert by_id["dyndolod-resources"]["source"]["mod_id"] == 52897
    assert by_id["dyndolod-resources"]["install"] == "fomod-defaults"
    assert by_id["dyndolod-dll-ng"]["source"]["mod_id"] == 97720
    assert by_id["dyndolod-dll-ng"]["install"] == "plain"
    for c in companions:
        assert c["install"] in ("plain", "fomod-defaults")
        assert c["source"]["type"] == "nexus"


def test_every_companion_mods_id_referenced_by_a_tool_exists_in_the_catalogue():
    catalog = tools.load_catalog()
    companion_ids = {c["id"] for c in tools.load_companion_catalog()}
    for entry in catalog:
        for cid in entry.get("companion_mods") or []:
            assert cid in companion_ids, f"{entry['id']} references unknown companion {cid!r}"


# -- companion mod install --------------------------------------------------------------


def _resolved(mod_id: int, file_id: int, filename: str, version: str) -> tools.Resolved:
    return tools.Resolved(
        download_url=f"https://example.test/{filename}",
        filename=filename,
        version=version,
        source_kind="nexus",
        nexus_domain="skyrimspecialedition",
        nexus_mod_id=mod_id,
        nexus_file_id=file_id,
    )


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_install_companion_mods_writes_folder_ledger_and_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mo2_dir = tmp_path / "inst"
    (mo2_dir / "mods").mkdir(parents=True)
    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.save()

    archive = tmp_path / "src" / "DynDOLOD Resources SE.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("meshes/x.nif", b"\x00\x01")

    fake_companion = {
        "id": "dyndolod-resources",
        "name": "DynDOLOD Resources SE",
        "source": {
            "type": "nexus",
            "domain": "skyrimspecialedition",
            "mod_id": 52897,
            "file": "latest-main",
        },
        "install": "plain",
    }
    monkeypatch.setattr(tools, "load_companion_catalog", lambda: [fake_companion])
    monkeypatch.setattr(
        tools, "resolve_source", lambda entry, client: _resolved(52897, 747879, archive.name, "Alpha-59")
    )
    monkeypatch.setattr(tools, "_download_cached", lambda url, dest, **kw: archive)

    entry = {"id": "dyndolod", "companion_mods": ["dyndolod-resources"]}
    records, ok = tools._install_companion_mods(entry, mo2_dir, client=None, led=led, force=False)

    assert ok is True
    assert len(records) == 1
    assert records[0]["id"] == "dyndolod-resources"
    assert records[0]["folder"] == "DynDOLOD Resources SE"

    mod_dir = mo2_dir / "mods" / "DynDOLOD Resources SE"
    assert (mod_dir / "meshes" / "x.nif").is_file()
    assert (mod_dir / "meta.ini").exists()
    assert "owner: tool:dyndolod" in (mod_dir / "meta.ini").read_text(encoding="utf-8")
    assert led.owners_of("DynDOLOD Resources SE") == ["tool:dyndolod"]

    dl = mo2_dir / "downloads" / archive.name
    assert dl.is_file()
    meta_text = dl.with_name(dl.name + ".meta").read_text(encoding="utf-8")
    assert "modID = 52897" in meta_text or "modID=52897" in meta_text.replace(" ", "")


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_install_companion_mods_skips_reinstall_at_the_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mo2_dir = tmp_path / "inst"
    (mo2_dir / "mods").mkdir(parents=True)
    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.save()

    archive = tmp_path / "src" / "DynDOLOD DLL NG and Scripts.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SKSE/Plugins/DynDOLOD.DLL", b"\x00")

    fake_companion = {
        "id": "dyndolod-dll-ng",
        "name": "DynDOLOD DLL NG and Scripts",
        "source": {
            "type": "nexus",
            "domain": "skyrimspecialedition",
            "mod_id": 97720,
            "file": "latest-main",
        },
        "install": "plain",
    }
    monkeypatch.setattr(tools, "load_companion_catalog", lambda: [fake_companion])
    monkeypatch.setattr(
        tools, "resolve_source", lambda entry, client: _resolved(97720, 793857, archive.name, "Alpha-42")
    )
    downloads: list[str] = []

    def fake_download(url, dest, **kw):
        downloads.append(url)
        return archive

    monkeypatch.setattr(tools, "_download_cached", fake_download)

    entry = {"id": "dyndolod", "companion_mods": ["dyndolod-dll-ng"]}
    tools._install_companion_mods(entry, mo2_dir, client=None, led=led, force=False)
    assert len(downloads) == 1

    records, ok = tools._install_companion_mods(entry, mo2_dir, client=None, led=led, force=False)
    assert ok is True
    assert len(downloads) == 1  # no second download: same version, folder still present
    assert records[0]["folder"] == "DynDOLOD DLL NG and Scripts"


_FOMOD_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<config>
  <moduleName>Test Companion FOMOD</moduleName>
  <requiredInstallFiles>
    <folder source="Core" priority="0" />
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Stage">
      <optionalFileGroups order="Explicit">
        <group name="Extra" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="ExtraPack">
              <description>Extra</description>
              <files>
                <file source="Extra/extra.esp" />
              </files>
              <typeDescriptor>
                <type name="Optional" />
              </typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_install_companion_mods_installs_a_fomod_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # DynDOLOD Resources SE (install: "fomod-defaults") is a real FOMOD, not a plain
    # archive -- this exercises the same fomod.evaluate()-driven branch of
    # installer._install_one that `c2wj install` uses, just for a companion mod.
    archive = tmp_path / "src" / "DynDOLOD Resources SE.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", _FOMOD_CONFIG)
        zf.writestr("Core/core.esp", b"\x00")
        zf.writestr("Extra/extra.esp", b"\x00")

    mo2_dir = tmp_path / "inst"
    (mo2_dir / "mods").mkdir(parents=True)
    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.save()

    fake_companion = {
        "id": "dyndolod-resources",
        "name": "DynDOLOD Resources SE",
        "source": {
            "type": "nexus",
            "domain": "skyrimspecialedition",
            "mod_id": 52897,
            "file": "latest-main",
        },
        "install": "fomod-defaults",
    }
    monkeypatch.setattr(tools, "load_companion_catalog", lambda: [fake_companion])
    monkeypatch.setattr(
        tools, "resolve_source", lambda entry, client: _resolved(52897, 747879, archive.name, "Alpha-59")
    )
    monkeypatch.setattr(tools, "_download_cached", lambda url, dest, **kw: archive)

    entry = {"id": "dyndolod", "companion_mods": ["dyndolod-resources"]}
    records, ok = tools._install_companion_mods(entry, mo2_dir, client=None, led=led, force=False)

    assert ok is True
    assert records[0]["strategy"] == "fomod-defaults"
    mod_dir = mo2_dir / "mods" / "DynDOLOD Resources SE"
    # requiredInstallFiles always land regardless of any choice.
    assert (mod_dir / "core.esp").is_file()


# -- tools remove ------------------------------------------------------------------------


def test_cmd_tools_remove_deletes_tool_dir_executables_and_solely_owned_companions(
    tmp_path: Path,
):
    mo2_dir = tmp_path / "inst"
    tool_dir = mo2_dir / "Tools" / "dyndolod"
    (mo2_dir / "mods" / "DynDOLOD Resources SE").mkdir(parents=True)
    tool_dir.mkdir(parents=True)
    (tool_dir / "DynDOLODx64.exe").write_bytes(b"x")

    binary = tools._fwd(str(tool_dir.resolve())) + "/DynDOLODx64.exe"
    ini_path = mo2_dir / "ModOrganizer.ini"
    ini_path.write_text(
        "[General]\n\n"
        "[customExecutables]\n"
        "size=1\n"
        "1\\title=DynDOLOD (SSE)\n"
        f"1\\binary={binary}\n"
        "1\\arguments=-sse\n"
        "1\\workingDirectory=\n",
        encoding="utf-8",
    )

    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.set_mod_owner("DynDOLOD Resources SE", ledger_mod.tool_owner("dyndolod"))
    led.data["tools"]["dyndolod"] = {
        "name": "DynDOLOD",
        "version": "Alpha-211",
        "executables": [
            {
                "title": "DynDOLOD (SSE)",
                "binary": "DynDOLODx64.exe",
                "arguments": "-sse",
                "workingDirectory": "tool",
            }
        ],
        "companion_mods": [
            {"id": "dyndolod-resources", "name": "DynDOLOD Resources SE", "folder": "DynDOLOD Resources SE"}
        ],
    }
    led.save()

    args = argparse.Namespace(ids=["dyndolod"], mo2_dir=str(mo2_dir))
    rc = tools.cmd_tools_remove(args)

    assert rc == 0
    assert not tool_dir.exists()
    assert not (mo2_dir / "mods" / "DynDOLOD Resources SE").exists()
    data = json.loads((mo2_dir / ledger_mod.LEDGER_NAME).read_text(encoding="utf-8"))
    assert "dyndolod" not in data["tools"]
    assert "DynDOLOD Resources SE" not in data["mods"]
    assert "DynDOLODx64.exe" not in ini_path.read_text(encoding="utf-8")


def test_cmd_tools_remove_keeps_a_companion_mod_owned_by_something_else(tmp_path: Path):
    mo2_dir = tmp_path / "inst"
    (mo2_dir / "mods" / "Shared Companion").mkdir(parents=True)
    (mo2_dir / "Tools" / "dyndolod").mkdir(parents=True)

    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.set_mod_owner("Shared Companion", ledger_mod.tool_owner("dyndolod"))
    led.add_mod_owner("Shared Companion", "user")
    led.data["tools"]["dyndolod"] = {
        "name": "DynDOLOD",
        "version": "Alpha-211",
        "executables": [],
        "companion_mods": [{"id": "dyndolod-resources", "name": "Shared Companion", "folder": "Shared Companion"}],
    }
    led.save()

    rc = tools.cmd_tools_remove(argparse.Namespace(ids=["dyndolod"], mo2_dir=str(mo2_dir)))

    assert rc == 0
    assert (mo2_dir / "mods" / "Shared Companion").is_dir()
    data = json.loads((mo2_dir / ledger_mod.LEDGER_NAME).read_text(encoding="utf-8"))
    assert data["mods"]["Shared Companion"]["owners"] == ["user"]


def test_cmd_tools_remove_of_an_uninstalled_tool_is_a_no_op(tmp_path: Path):
    mo2_dir = tmp_path / "inst"
    mo2_dir.mkdir()
    led = ledger_mod.Ledger(mo2_dir)
    led.save()

    rc = tools.cmd_tools_remove(argparse.Namespace(ids=["dyndolod"], mo2_dir=str(mo2_dir)))
    assert rc == 0


# -- tools list ----------------------------------------------------------------------


def test_cmd_tools_list_prints_a_companion_mods_section(capsys: pytest.CaptureFixture[str]):
    rc = tools.cmd_tools_list(argparse.Namespace(mo2_dir=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "companion mods:" in out
    assert "dyndolod-resources" in out
    assert "dyndolod-dll-ng" in out
