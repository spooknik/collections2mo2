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

from collections2mo2 import ledger as ledger_mod
from collections2mo2 import tools
from collections2mo2.sevenzip import TOOLS_DIR

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


def test_xedit_and_dyndolod_executables_carry_the_data_path_switch():
    # Regression test for the "launching xEdit/DynDOLOD from MO2 in a Stock Game
    # instance shows only base-game plugins" bug: without -D:"<gamePath>\Data", these
    # xEdit-family tools locate the game via the registry (the real Steam install)
    # instead of MO2's VFS-managed Stock Game copy.
    catalog = tools.load_catalog()
    xedit = next(e for e in catalog if e["id"] == "xedit")
    dyndolod = next(e for e in catalog if e["id"] == "dyndolod")
    for exe in xedit["executables"] + dyndolod["executables"]:
        assert '-D:"{game_data}"' in exe["arguments"]


# -- placeholder expansion / Qt-escaping for `arguments` --------------------------------


def test_expand_arg_template_matches_a_verified_wabbajack_stock_game_instance():
    # Byte-for-byte against E:\Games\Lorerim\ModOrganizer.ini (a working Wabbajack
    # Stock Game build): `12\arguments=-D:\"E:\\Games\\Lorerim\\Stock Game\\Data\" -sse`
    # for TexGen -- MO2/Qt doubles every backslash then escapes every quote.
    expanded = tools._expand_arg_template(
        '-D:"{game_data}" -sse',
        game_path="E:/Games/Lorerim/Stock Game",
        tool_dir_fwd="E:/Games/Lorerim/Tools/dyndolod",
    )
    assert expanded == '-D:\\"E:\\\\Games\\\\Lorerim\\\\Stock Game\\\\Data\\" -sse'


def test_expand_arg_template_empty_game_path_yields_empty_game_data():
    expanded = tools._expand_arg_template(
        '-D:"{game_data}" -sse', game_path="", tool_dir_fwd="E:/Games/GTS/Tools/xedit"
    )
    assert expanded == '-D:\\"\\" -sse'


def test_build_executable_blocks_expands_and_escapes_xedit_arguments(tmp_path: Path):
    catalog = tools.load_catalog()
    xedit = next(e for e in catalog if e["id"] == "xedit")
    tool_dir = tmp_path / "Tools" / "xedit"
    blocks = tools.build_executable_blocks(xedit, tool_dir, "E:/Games/GTS/Stock Game")
    normal = next(b for b in blocks if b["title"] == "xEdit (SSE)")
    tool_dir_fwd = tools._fwd(str(tool_dir.resolve()))
    assert normal["arguments"] == '-D:\\"E:\\\\Games\\\\GTS\\\\Stock Game\\\\Data\\" -sse'
    assert normal["binary"] == f"{tool_dir_fwd}/xTESEdit64.exe"


# -- replace_executables (tools refresh) -------------------------------------------------


def _write_ini(path: Path, *entries: tuple[str, str, str, str]) -> None:
    lines = ["[General]", "gamePath=", "", "[customExecutables]", f"size={len(entries)}"]
    for i, (title, binary, arguments, working_dir) in enumerate(entries, start=1):
        lines += [
            f"{i}\\title={title}",
            f"{i}\\binary={binary}",
            f"{i}\\arguments={arguments}",
            f"{i}\\workingDirectory={working_dir}",
            f"{i}\\hide=false",
            f"{i}\\ownicon=true",
            f"{i}\\steamAppID=",
            f"{i}\\toolbar=true",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_replace_executables_updates_stale_arguments_in_place(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    _write_ini(
        ini_path,
        ("xEdit (SSE)", "E:/GTS/Tools/xedit/xTESEdit64.exe", "-sse", ""),
        ("Some User Tool", "E:/GTS/tools/thing.exe", "-x", ""),
    )

    new_blocks = [
        {
            "title": "xEdit (SSE)",
            "binary": "E:/GTS/Tools/xedit/xTESEdit64.exe",
            "arguments": '-D:\\"E:\\\\GTS\\\\Stock Game\\\\Data\\" -sse',
            "workingDirectory": "E:/GTS/Tools/xedit",
        }
    ]
    changed = tools.replace_executables(ini_path, new_blocks)

    assert changed == ["xEdit (SSE)"]
    text = ini_path.read_text(encoding="utf-8")
    assert '1\\arguments=-D:\\"E:\\\\GTS\\\\Stock Game\\\\Data\\" -sse' in text
    assert "1\\workingDirectory=E:/GTS/Tools/xedit" in text
    # The unrelated user-registered entry is preserved verbatim.
    assert "2\\title=Some User Tool" in text
    assert "2\\arguments=-x" in text
    # size= and hide/ownicon/etc. of the updated entry are untouched.
    assert "size=2" in text
    assert "1\\hide=false" in text


def test_replace_executables_appends_a_block_whose_title_is_not_yet_registered(
    tmp_path: Path,
):
    ini_path = tmp_path / "ModOrganizer.ini"
    _write_ini(ini_path, ("Other Tool", "E:/GTS/tools/other.exe", "", ""))

    new_blocks = [
        {
            "title": "TexGen (SSE)",
            "binary": "E:/GTS/Tools/dyndolod/TexGenx64.exe",
            "arguments": '-D:\\"E:\\\\GTS\\\\Stock Game\\\\Data\\" -sse',
            "workingDirectory": "E:/GTS/Tools/dyndolod",
        }
    ]
    changed = tools.replace_executables(ini_path, new_blocks)

    assert changed == ["TexGen (SSE)"]
    text = ini_path.read_text(encoding="utf-8")
    assert "size=2" in text
    assert "2\\title=TexGen (SSE)" in text


def test_replace_executables_is_idempotent(tmp_path: Path):
    ini_path = tmp_path / "ModOrganizer.ini"
    _write_ini(ini_path, ("xEdit (SSE)", "E:/GTS/Tools/xedit/xTESEdit64.exe", "-sse", ""))
    new_blocks = [
        {
            "title": "xEdit (SSE)",
            "binary": "E:/GTS/Tools/xedit/xTESEdit64.exe",
            "arguments": '-D:\\"E:\\\\GTS\\\\Stock Game\\\\Data\\" -sse',
            "workingDirectory": "E:/GTS/Tools/xedit",
        }
    ]
    tools.replace_executables(ini_path, new_blocks)
    text_after_first = ini_path.read_text(encoding="utf-8")

    changed_second = tools.replace_executables(ini_path, new_blocks)

    assert changed_second == []
    assert ini_path.read_text(encoding="utf-8") == text_after_first


# -- cmd_tools_refresh --------------------------------------------------------------------


def test_cmd_tools_refresh_rewrites_only_the_installed_tools_arguments(tmp_path: Path):
    mo2_dir = tmp_path / "inst"
    mo2_dir.mkdir()
    tool_dir = mo2_dir / "Tools" / "xedit"
    (tool_dir).mkdir(parents=True)
    (tool_dir / "xTESEdit64.exe").write_bytes(b"x")

    binary = tools._fwd(str(tool_dir.resolve())) + "/xTESEdit64.exe"
    ini_path = mo2_dir / "ModOrganizer.ini"
    _write_ini(
        ini_path,
        ("xEdit (SSE)", binary, "-sse", ""),
        ("xEdit (SSE) - QuickAutoClean", binary, "-sse -quickautoclean", ""),
        ("User Tool", "E:/other/thing.exe", "-x", ""),
    )
    # gamePath must be set for {game_data} to expand to something real.
    ini_text = ini_path.read_text(encoding="utf-8").replace(
        "gamePath=", f"gamePath=@ByteArray({mo2_dir / 'Stock Game'!s})"
    )
    ini_path.write_text(ini_text, encoding="utf-8")

    led = ledger_mod.Ledger(mo2_dir)
    led.set_game(domain="skyrimspecialedition", mo2_name="SkyrimSE")
    led.data["tools"]["xedit"] = {
        "name": "xEdit (SSEEdit)",
        "version": "xedit-4.1.5f",
        "executables": tools._catalog_by_id(tools.load_catalog())["xedit"]["executables"],
        "companion_mods": [],
    }
    led.save()

    rc = tools.cmd_tools_refresh(argparse.Namespace(ids=[], mo2_dir=str(mo2_dir)))
    assert rc == 0

    text = ini_path.read_text(encoding="utf-8")
    assert '-D:\\"' in text
    assert "Stock Game\\\\Data" in text
    # The unrelated user tool is untouched.
    assert "3\\title=User Tool" in text
    assert "3\\arguments=-x" in text


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
        tools,
        "resolve_source",
        lambda entry, client: _resolved(52897, 747879, archive.name, "Alpha-59"),
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
        tools,
        "resolve_source",
        lambda entry, client: _resolved(97720, 793857, archive.name, "Alpha-42"),
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
    # installer._install_one that `c2mo2 install` uses, just for a companion mod.
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
        tools,
        "resolve_source",
        lambda entry, client: _resolved(52897, 747879, archive.name, "Alpha-59"),
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
            {
                "id": "dyndolod-resources",
                "name": "DynDOLOD Resources SE",
                "folder": "DynDOLOD Resources SE",
            }
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
        "companion_mods": [
            {"id": "dyndolod-resources", "name": "Shared Companion", "folder": "Shared Companion"}
        ],
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
