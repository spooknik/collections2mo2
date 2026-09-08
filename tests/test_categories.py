"""Nexus category import: the MO2 id table, the .dat files and the meta.ini keys.

The sample tables below are the first six lines of a real user instance's
`categories.dat` / `nexuscatmap.dat` (one MO2 had written itself after "import Nexus
categories"), so the renderers are checked against MO2's own output rather than
against our idea of it. Nothing here touches the network: the Nexus client is a stub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_layers import make_instance

from collections2mo2 import categories, installer, profile
from collections2mo2.nexus import NexusError
from collections2mo2.reporter import NullReporter

SAMPLE_CATEGORIES_DAT = (
    "1|Skyrim Special Edition|0\n"
    "2|Buildings|0\n"
    "3|Gameplay|0\n"
    "4|Guilds/Factions|0\n"
    "5|Body, Face, and Hair|0\n"
    "6|Items and Objects - Player|0\n"
)
SAMPLE_NEXUSCATMAP_DAT = (
    "1|Skyrim Special Edition|20\n"
    "2|Buildings|22\n"
    "3|Gameplay|24\n"
    "4|Guilds/Factions|25\n"
    "5|Body, Face, and Hair|26\n"
    "6|Items and Objects - Player|27\n"
)

# What `GET /v1/games/skyrimspecialedition.json` returns for those six, out of order
# on purpose: MO2 sorts by category id, not by the order Nexus happens to list them.
SAMPLE_GAME_JSON = {
    "categories": [
        {"category_id": 24, "name": "Gameplay", "parent_category": 20},
        {"category_id": 20, "name": "Skyrim Special Edition", "parent_category": False},
        {"category_id": 27, "name": "Items and Objects - Player", "parent_category": 20},
        {"category_id": 22, "name": "Buildings", "parent_category": 20},
        {"category_id": 26, "name": "Body, Face, and Hair", "parent_category": 20},
        {"category_id": 25, "name": "Guilds/Factions", "parent_category": 20},
    ]
}


class FakeClient:
    """Stands in for NexusClient: the two calls categories.py makes, nothing else."""

    def __init__(self, game=None, mod_files=None, game_error=None, graphql_error=None):
        self.game = game if game is not None else SAMPLE_GAME_JSON
        self.mod_files = mod_files or []
        self.game_error = game_error
        self.graphql_error = graphql_error
        self.calls: list[str] = []

    def game_info(self, domain):
        self.calls.append(f"game_info:{domain}")
        if self.game_error:
            raise self.game_error
        return self.game

    def graphql(self, query, variables=None):
        self.calls.append(f"graphql:{(variables or {}).get('slug')}")
        if self.graphql_error:
            raise self.graphql_error
        return {"collectionRevision": {"modFiles": self.mod_files}}


def _mod_file(mod_id, category_id, name):
    return {
        "file": {
            "modId": mod_id,
            "mod": {
                "modId": mod_id,
                "modCategory": {"id": f"{category_id},1704", "name": name},
            },
        }
    }


# ------------------------------------------------------------------- the table


def test_from_nexus_numbers_categories_by_ascending_nexus_id():
    table = categories.CategoryTable.from_nexus(
        categories.fetch_game_categories(FakeClient(), "skyrimspecialedition")
    )
    assert [e.mo2_id for e in table.entries] == [1, 2, 3, 4, 5, 6]
    assert [e.nexus_id for e in table.entries] == [20, 22, 24, 25, 26, 27]
    assert table.mo2_id_for_nexus(26) == 5
    assert table.mo2_id_for_nexus(999) is None


def test_renderers_reproduce_the_dat_files_mo2_itself_wrote():
    table = categories.CategoryTable.from_nexus(
        categories.fetch_game_categories(FakeClient(), "skyrimspecialedition")
    )
    assert categories.render_categories_dat(table) == SAMPLE_CATEGORIES_DAT
    assert categories.render_nexuscatmap_dat(table) == SAMPLE_NEXUSCATMAP_DAT


def test_from_instance_reads_the_instances_own_numbering(tmp_path: Path):
    (tmp_path / "nexuscatmap.dat").write_text(SAMPLE_NEXUSCATMAP_DAT, encoding="utf-8")
    table = categories.CategoryTable.from_instance(tmp_path)
    assert table is not None
    assert len(table) == 6
    assert table.mo2_id_for_nexus(24) == 3
    assert table.nexus_id_for_name("  gAMEplay ") == 24


def test_from_instance_is_none_without_a_file_and_tolerates_junk(tmp_path: Path):
    assert categories.CategoryTable.from_instance(tmp_path) is None
    (tmp_path / "nexuscatmap.dat").write_text("", encoding="utf-8")
    assert categories.CategoryTable.from_instance(tmp_path) is None
    (tmp_path / "nexuscatmap.dat").write_text(
        "not a row\n1|Gameplay|24\nx|Bad|y\n2|Short\n", encoding="utf-8"
    )
    table = categories.CategoryTable.from_instance(tmp_path)
    assert table is not None
    assert len(table) == 1
    assert table.mo2_id_for_nexus(24) == 1


def test_table_json_round_trip_keeps_the_mo2_ids():
    table = categories.CategoryTable([categories.CategoryEntry(7, 33, "NPC")])
    again = categories.CategoryTable.from_json(table.to_json())
    assert again.mo2_id_for_nexus(33) == 7


def test_from_json_numbers_a_plain_game_category_list():
    table = categories.CategoryTable.from_json(
        [{"nexus_id": 33, "name": "NPC", "parent": 20}, {"nexus_id": 24, "name": "Gameplay"}]
    )
    assert table.mo2_id_for_nexus(24) == 1
    assert table.mo2_id_for_nexus(33) == 2


# --------------------------------------------------------------- the .dat files


def test_write_instance_files_writes_both_then_never_overwrites(tmp_path: Path):
    table = categories.CategoryTable.from_json([{"nexus_id": 24, "name": "Gameplay"}])
    written = categories.write_instance_files(tmp_path, table)
    assert [p.name for p in written] == ["categories.dat", "nexuscatmap.dat"]

    other = categories.CategoryTable.from_json([{"nexus_id": 99, "name": "Other"}])
    assert categories.write_instance_files(tmp_path, other) == []
    assert (tmp_path / "nexuscatmap.dat").read_text(encoding="utf-8") == "1|Gameplay|24\n"


def test_write_instance_files_leaves_a_half_pair_alone(tmp_path: Path):
    # MO2 wrote one of them; a fresh pair would renumber what its meta.ini files mean.
    (tmp_path / "categories.dat").write_text("1|Mine|0\n", encoding="utf-8")
    table = categories.CategoryTable.from_json([{"nexus_id": 24, "name": "Gameplay"}])
    assert categories.write_instance_files(tmp_path, table) == []
    assert not (tmp_path / "nexuscatmap.dat").exists()


# ------------------------------------------------------------------- fetching


def test_mod_category_id_takes_the_part_before_the_game_id():
    assert categories.parse_mod_category_id("24,1704") == 24
    assert categories.parse_mod_category_id("33") == 33
    assert categories.parse_mod_category_id(33) == 33
    assert categories.parse_mod_category_id("") is None
    assert categories.parse_mod_category_id(None) is None
    assert categories.parse_mod_category_id({"id": 1}) is None


def test_fetch_mod_categories_skips_files_without_a_mod_or_category():
    client = FakeClient(
        mod_files=[
            _mod_file(169247, 33, "NPC"),
            {"file": {"modId": 5, "mod": None}},
            {"file": {"modId": 6, "mod": {"modId": 6, "modCategory": None}}},
            {},
        ]
    )
    assert categories.fetch_mod_categories(client, "h2uqa3", 68) == {169247: (33, "NPC")}


# -------------------------------------------------------------------- resolve


def _layer_categories():
    return categories.LayerCategories(
        domain="skyrimspecialedition",
        game_categories=[{"nexus_id": 24, "name": "Gameplay"}, {"nexus_id": 33, "name": "NPC"}],
        mods={"169247": {"nexus_id": 33, "name": "NPC"}},
    )


def test_resolve_prefers_the_graphql_id_over_the_manifest_name():
    layer = _layer_categories()
    table = categories.CategoryTable.from_json(layer.game_categories)
    mod = {"source": {"modId": 169247}, "details": {"category": "Gameplay"}}
    assert layer.resolve(mod, table) == (2, 33)


def test_resolve_falls_back_to_an_exact_manifest_category_name():
    layer = _layer_categories()
    table = categories.CategoryTable.from_json(layer.game_categories)
    mod = {"source": {"modId": 1}, "details": {"category": "gameplay"}}
    assert layer.resolve(mod, table) == (1, 24)


def test_resolve_gives_up_on_a_stale_manifest_category_name():
    # "Gameplay Effects and Changes" is what Nexus called the category when the
    # revision was cut; the API now says "Gameplay". No partial matching.
    layer = _layer_categories()
    table = categories.CategoryTable.from_json(layer.game_categories)
    mod = {"source": {"modId": 1}, "details": {"category": "Gameplay Effects and Changes"}}
    assert layer.resolve(mod, table) is None
    assert layer.resolve({"source": {"modId": 2}}, table) is None


def test_resolve_reports_a_nexus_id_the_instance_table_does_not_know():
    layer = _layer_categories()
    table = categories.CategoryTable.from_json([{"nexus_id": 24, "name": "Gameplay"}])
    mod = {"source": {"modId": 169247}}
    assert layer.resolve(mod, table) == (0, 33)


# ------------------------------------------------------------------- meta.ini


def _meta(tmp_path: Path, text: str, newline: str = "\n") -> Path:
    path = tmp_path / "meta.ini"
    path.write_text(text.replace("\n", newline), encoding="utf-8", newline="")
    return path


def test_apply_meta_ini_fills_in_a_fresh_uncategorised_file(tmp_path: Path):
    path = _meta(tmp_path, "[General]\nmodid=1\ncategory=0\nvalidated=true\n")
    assert categories.apply_meta_ini(path, 9, 33) is True
    assert path.read_text(encoding="utf-8") == (
        '[General]\nmodid=1\ncategory="9,"\nnexusCategory=33\nvalidated=true\n'
    )
    # Idempotent: a second pass changes nothing and does not rewrite the file.
    assert categories.apply_meta_ini(path, 9, 33) is False


def test_apply_meta_ini_keeps_a_category_the_user_picked_in_mo2(tmp_path: Path):
    path = _meta(tmp_path, '[General]\ncategory="41,"\nnexusCategory=0\n')
    assert categories.apply_meta_ini(path, 9, 33) is True
    text = path.read_text(encoding="utf-8")
    assert 'category="41,"' in text
    assert "nexusCategory=33" in text
    # ...unless the caller insists.
    assert categories.apply_meta_ini(path, 9, 33, force=True) is True
    assert 'category="9,"' in path.read_text(encoding="utf-8")


def test_apply_meta_ini_adds_missing_keys_at_the_end_of_general(tmp_path: Path):
    path = _meta(tmp_path, "[General]\nmodid=1\n\n[installedFiles]\n1\\modid=1\n")
    assert categories.apply_meta_ini(path, 9, 33) is True
    text = path.read_text(encoding="utf-8")
    assert text.index('category="9,"') < text.index("[installedFiles]")
    assert text.index("nexusCategory=33") < text.index("[installedFiles]")


def test_apply_meta_ini_keeps_crlf(tmp_path: Path):
    path = _meta(tmp_path, "[General]\nmodid=1\ncategory=0\n", newline="\r\n")
    assert categories.apply_meta_ini(path, 9, 33) is True
    raw = path.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.endswith(b"\r\n")


def test_apply_meta_ini_writes_a_bare_zero_when_the_table_has_no_mo2_id(tmp_path: Path):
    path = _meta(tmp_path, "[General]\ncategory=0\n")
    assert categories.apply_meta_ini(path, 0, 33) is True
    assert path.read_text(encoding="utf-8") == "[General]\ncategory=0\nnexusCategory=33\n"


def test_apply_meta_ini_ignores_a_file_without_a_general_section(tmp_path: Path):
    path = _meta(tmp_path, "[installedFiles]\n1\\modid=1\n")
    assert categories.apply_meta_ini(path, 9, 33) is False
    assert categories.apply_meta_ini(tmp_path / "nope.ini", 9, 33) is False


# ---------------------------------------------------------------- prepare_layer


def test_prepare_layer_writes_the_json_and_seeds_the_instance(tmp_path: Path):
    inst = tmp_path / "inst"
    inst.mkdir()
    out = tmp_path / "c2mo2" / "h2uqa3-68.categories.json"
    client = FakeClient(mod_files=[_mod_file(169247, 26, "Body, Face, and Hair")])
    layer = categories.prepare_layer(
        client,
        domain="skyrimspecialedition",
        slug="h2uqa3",
        revision=68,
        manifest={"mods": [{"source": {"modId": 169247}}]},
        out_json=out,
        instance_dir=inst,
        rep=NullReporter(),
    )
    assert layer is not None
    assert layer.mods["169247"]["nexus_id"] == 26
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["domain"] == "skyrimspecialedition"
    assert len(data["game_categories"]) == 6
    assert (inst / "categories.dat").read_text(encoding="utf-8") == SAMPLE_CATEGORIES_DAT
    assert categories.load_layer(out).mods == layer.mods


@pytest.mark.parametrize("field", ["game_error", "graphql_error"])
def test_prepare_layer_swallows_a_nexus_failure(tmp_path: Path, field: str):
    class Rep(NullReporter):
        def __init__(self):
            self.warnings: list[str] = []

        def warn(self, msg: str) -> None:
            self.warnings.append(msg)

    rep = Rep()
    client = FakeClient(**{field: NexusError("Nexus is down")})
    out = tmp_path / "cats.json"
    assert (
        categories.prepare_layer(
            client,
            domain="skyrimspecialedition",
            slug="h2uqa3",
            revision=68,
            manifest={"mods": []},
            out_json=out,
            instance_dir=tmp_path,
            rep=rep,
        )
        is None
    )
    assert not out.exists()
    assert len(rep.warnings) == 1
    assert "Nexus is down" in rep.warnings[0]


def test_load_layer_is_none_for_a_missing_or_broken_file(tmp_path: Path):
    assert categories.load_layer(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert categories.load_layer(bad) is None


# ------------------------------------------------------------------- installer


META_WITHOUT_CATEGORY = """[General]
modid=169247
version=0.2.1.0
newestVersion=
category=0
installationFile=Bandits.zip
repository=Nexus
gameName=SkyrimSE
comments=
notes=
nexusFileStatus=1
hasCustomURL=false
validated=true
converted=false

[installedFiles]
1\\modid=169247
1\\fileid=707747
size=1
"""


def _write_meta(tmp_path: Path, category):
    dest = tmp_path / "mod"
    dest.mkdir()
    installer._write_meta_ini(
        dest,
        {"mod_id": 169247, "file_id": 707747, "file_name": "Bandits.zip"},
        {"version": "0.2.1.0"},
        "SkyrimSE",
        None,
        category,
    )
    return (dest / "meta.ini").read_text(encoding="utf-8")


def test_installer_meta_ini_is_unchanged_without_a_category(tmp_path: Path):
    assert _write_meta(tmp_path, None) == META_WITHOUT_CATEGORY


def test_installer_meta_ini_carries_both_category_keys(tmp_path: Path):
    expected = META_WITHOUT_CATEGORY.replace("category=0\n", 'category="9,"\nnexusCategory=33\n')
    assert _write_meta(tmp_path, (9, 33)) == expected


def test_installer_meta_ini_keeps_a_bare_zero_for_an_unmapped_category(tmp_path: Path):
    expected = META_WITHOUT_CATEGORY.replace("category=0\n", "category=0\nnexusCategory=33\n")
    assert _write_meta(tmp_path, (0, 33)) == expected


# ------------------------------------------------------- render_instance top-up


def _entry(folder: str, md5: str) -> dict:
    return {
        "name": folder,
        "folder": folder,
        "tag": folder.lower(),
        "md5": md5,
        "phase": 0,
        "optional": False,
        "install_mode": "fresh",
        "strategy": "data",
        "plugins": [],
    }


def _mod(name: str, md5: str, mod_id: int) -> dict:
    return {
        "name": name,
        "phase": 0,
        "source": {"tag": name.lower(), "md5": md5, "modId": mod_id},
        "details": {"category": "Gameplay"},
    }


def test_render_instance_tops_up_meta_ini_from_a_layers_categories_json(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {
                    "mods": [_mod("A", "md5a", 169247), _mod("B", "md5b", 1)],
                    "info": {},
                },
                "entries": [_entry("A", "md5a"), _entry("B", "md5b")],
            }
        ],
    )
    for folder in ("A", "B"):
        (inst / "mods" / folder / "meta.ini").write_text(
            "[General]\nmodid=1\ncategory=0\n", encoding="utf-8"
        )
    (inst / "c2mo2" / "base-1.categories.json").write_text(
        json.dumps(
            {
                "domain": "skyrimspecialedition",
                "game_categories": [
                    {"nexus_id": 24, "name": "Gameplay"},
                    {"nexus_id": 33, "name": "NPC"},
                ],
                "mods": {"169247": {"nexus_id": 33, "name": "NPC"}},
            }
        ),
        encoding="utf-8",
    )

    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )

    # A is categorised from GraphQL, B from its manifest category name; the instance
    # got the .dat pair it had none of.
    assert (inst / "mods" / "A" / "meta.ini").read_text(encoding="utf-8") == (
        '[General]\nmodid=1\ncategory="2,"\nnexusCategory=33\n'
    )
    assert (inst / "mods" / "B" / "meta.ini").read_text(encoding="utf-8") == (
        '[General]\nmodid=1\ncategory="1,"\nnexusCategory=24\n'
    )
    assert (inst / "nexuscatmap.dat").read_text(encoding="utf-8") == ("1|Gameplay|24\n2|NPC|33\n")


def test_render_instance_without_a_categories_json_touches_nothing(tmp_path: Path):
    inst, led = make_instance(
        tmp_path,
        [
            {
                "slug": "base",
                "revision": 1,
                "name": "Base List",
                "manifest": {"mods": [_mod("A", "md5a", 169247)], "info": {}},
                "entries": [_entry("A", "md5a")],
            }
        ],
    )
    (inst / "mods" / "A" / "meta.ini").write_text(
        "[General]\nmodid=1\ncategory=0\n", encoding="utf-8"
    )
    profile.render_instance(
        inst, led=led, keep_inis=True, reporter=NullReporter(), profile_name="TestProfile"
    )
    assert (inst / "mods" / "A" / "meta.ini").read_text(encoding="utf-8") == (
        "[General]\nmodid=1\ncategory=0\n"
    )
    assert not (inst / "nexuscatmap.dat").exists()
