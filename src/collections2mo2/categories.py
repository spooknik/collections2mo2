"""Nexus mod categories for MO2's mod list.

MO2 shows a category column and lets the user filter by it, but only if two things
line up: each mod's `meta.ini` carries `category="<mo2 id>,"` / `nexusCategory=<nexus id>`,
and the instance root holds the two lookup tables MO2's own "import Nexus categories"
would have written:

    categories.dat     `<mo2 id>|<name>|<parent mo2 id>`
    nexuscatmap.dat    `<mo2 id>|<nexus name>|<nexus category id>`

MO2 numbers its own ids 1..n in ascending Nexus category id order and gives every
imported category parent 0, so a table built from `GET /v1/games/<domain>.json`
reproduces a real user instance's files byte for byte. We only ever *create* those
files when the instance has neither -- once MO2 or the user has a numbering, that
numbering wins, because the ids in every `meta.ini` are relative to it.

Categories are cosmetic. Every fetch here is wrapped so that a Nexus outage, a missing
API key or a non-Premium account costs the run a warning and nothing else.

The per-layer JSON (`c2mo2/<slug>-<rev>.categories.json`) written by `prepare_layer`
looks like::

    {"domain": "skyrimspecialedition",
     "fetched": "2026-09-08T12:00:00Z",
     "game_categories": [{"nexus_id": 20, "name": "Skyrim Special Edition",
                          "parent": null}, ...],
     "mods": {"169247": {"nexus_id": 33, "name": "NPC"}, ...}}

The `mods` map comes from GraphQL (authoritative, one entry per mod in the revision);
the manifest's own `details.category` is only a name and goes stale between Nexus
category renames, so it is used as a fallback and only on an exact, case-insensitive
name match.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .nexus import NexusClient, NexusError
from .reporter import Reporter, get_reporter

CATEGORIES_DAT = "categories.dat"
NEXUSCATMAP_DAT = "nexuscatmap.dat"

MOD_CATEGORY_QUERY = """
query($slug: String!, $rev: Int!) {
  collectionRevision(slug: $slug, revision: $rev, viewAdultContent: true) {
    modFiles { file { modId mod { modId modCategory { id name } } } }
  }
}
"""


# --------------------------------------------------------------------- the table


@dataclass(frozen=True)
class GameCategory:
    """One category as the Nexus v1 game endpoint reports it."""

    nexus_id: int
    name: str
    parent_nexus_id: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"nexus_id": self.nexus_id, "name": self.name, "parent": self.parent_nexus_id}


@dataclass(frozen=True)
class CategoryEntry:
    """One row of MO2's category tables: its own id, the Nexus id, the shared name."""

    mo2_id: int
    nexus_id: int
    name: str


def _norm(name: str) -> str:
    """Fold a category name for matching: case and runs of whitespace are noise."""
    return " ".join(str(name or "").split()).lower()


@dataclass
class CategoryTable:
    """The MO2 id <-> Nexus id mapping an instance is using."""

    entries: list[CategoryEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_nexus: dict[int, CategoryEntry] = {}
        self._by_name: dict[str, CategoryEntry] = {}
        for entry in self.entries:
            self._by_nexus.setdefault(entry.nexus_id, entry)
            self._by_name.setdefault(_norm(entry.name), entry)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    # -- constructors ------------------------------------------------------------

    @classmethod
    def from_nexus(cls, categories: Iterable[GameCategory]) -> CategoryTable:
        """Number the game's categories the way MO2's importer does: 1..n by Nexus id."""
        ordered = sorted(categories, key=lambda c: c.nexus_id)
        return cls(
            [
                CategoryEntry(mo2_id=i, nexus_id=c.nexus_id, name=c.name)
                for i, c in enumerate(ordered, start=1)
            ]
        )

    @classmethod
    def from_instance(cls, instance_dir: Path | str) -> CategoryTable | None:
        """Read the instance's own `nexuscatmap.dat`, or None when it has none.

        MO2 (or the user) may have renumbered; whatever is on disk is what the
        `category=` values in `meta.ini` mean, so it always beats a fresh table.
        """
        path = Path(instance_dir) / NEXUSCATMAP_DAT
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        entries: list[CategoryEntry] = []
        for line in text.splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            try:
                mo2_id, nexus_id = int(parts[0].strip()), int(parts[2].strip())
            except ValueError:
                continue
            entries.append(CategoryEntry(mo2_id=mo2_id, nexus_id=nexus_id, name=parts[1]))
        if not entries:
            return None
        entries.sort(key=lambda e: e.mo2_id)
        return cls(entries)

    @classmethod
    def from_json(cls, data: Any) -> CategoryTable:
        """Rebuild a table from `to_json()` output or from a stored game-category list.

        A list without `mo2_id` keys is a plain game-category dump, so it is numbered
        the same way `from_nexus` would.
        """
        rows = [r for r in (data or []) if isinstance(r, dict)]
        if any("mo2_id" in r for r in rows):
            entries: list[CategoryEntry] = []
            for row in rows:
                try:
                    entries.append(
                        CategoryEntry(
                            mo2_id=int(row["mo2_id"]),
                            nexus_id=int(row["nexus_id"]),
                            name=str(row.get("name") or ""),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            entries.sort(key=lambda e: e.mo2_id)
            return cls(entries)
        cats: list[GameCategory] = []
        for row in rows:
            try:
                cats.append(
                    GameCategory(
                        nexus_id=int(row["nexus_id"]),
                        name=str(row.get("name") or ""),
                        parent_nexus_id=(
                            int(row["parent"]) if isinstance(row.get("parent"), int) else None
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cls.from_nexus(cats)

    def to_json(self) -> list[dict[str, Any]]:
        return [{"mo2_id": e.mo2_id, "nexus_id": e.nexus_id, "name": e.name} for e in self.entries]

    # -- lookups -----------------------------------------------------------------

    def mo2_id_for_nexus(self, nexus_id: int) -> int | None:
        entry = self._by_nexus.get(int(nexus_id))
        return entry.mo2_id if entry else None

    def nexus_id_for_name(self, name: str) -> int | None:
        entry = self._by_name.get(_norm(name))
        return entry.nexus_id if entry else None


# ------------------------------------------------------------- the .dat files


def render_categories_dat(table: CategoryTable) -> str:
    """`<mo2 id>|<name>|0` lines. Every imported Nexus category is top level."""
    return "".join(f"{e.mo2_id}|{e.name}|0\n" for e in table.entries)


def render_nexuscatmap_dat(table: CategoryTable) -> str:
    return "".join(f"{e.mo2_id}|{e.name}|{e.nexus_id}\n" for e in table.entries)


def write_instance_files(
    instance_dir: Path | str, table: CategoryTable, rep: Reporter | None = None
) -> list[Path]:
    """Create `categories.dat` / `nexuscatmap.dat`, but only if the instance has neither.

    Half a pair would be worse than none (the two files have to agree on the ids), and
    overwriting either would silently re-map every `category=` already in a `meta.ini`,
    so an instance that has been near MO2's category importer is left alone.
    """
    root = Path(instance_dir)
    cat_path, map_path = root / CATEGORIES_DAT, root / NEXUSCATMAP_DAT
    if not table.entries or cat_path.exists() or map_path.exists():
        return []
    root.mkdir(parents=True, exist_ok=True)
    cat_path.write_text(render_categories_dat(table), encoding="utf-8", newline="\n")
    map_path.write_text(render_nexuscatmap_dat(table), encoding="utf-8", newline="\n")
    if rep is not None:
        rep.log(f"categories: wrote {len(table)} categories to categories.dat/nexuscatmap.dat")
    return [cat_path, map_path]


# ----------------------------------------------------------------- meta.ini


def meta_ini_lines(mo2_id: int, nexus_id: int) -> list[str]:
    """The two `[General]` lines MO2 writes for a categorised mod, in MO2's own order.

    `category` is a quoted comma-terminated *list*, so a single category is `"9,"`;
    mo2 id 0 means "the instance has no row for this Nexus category", and MO2 writes
    that as a bare `category=0`.
    """
    category = f'category="{mo2_id},"' if mo2_id > 0 else "category=0"
    return [category, f"nexusCategory={nexus_id}"]


def _split_key(line: str) -> str:
    key, sep, _ = line.partition("=")
    return key.strip().lower() if sep else ""


def _is_unset_category(value: str) -> bool:
    """True when `category=` still holds the placeholder we (or MO2) write for 'none'."""
    return value.strip().strip('"').strip(",").strip() in ("", "0")


def apply_meta_ini(
    meta_path: Path | str, mo2_id: int, nexus_id: int, *, force: bool = False
) -> bool:
    """Top up an existing mod's `meta.ini` with its category keys. True if it changed.

    Line based rather than configparser: MO2's meta.ini holds quoted multi-line
    descriptions and `@Variant(...)` blobs that a round trip through configparser
    would mangle. Without `force` a category the user picked in MO2 is left alone --
    only the placeholder 0 is filled in -- while `nexusCategory` is always topped up,
    since it records what Nexus says and never what the user chose.
    """
    path = Path(meta_path)
    try:
        # newline="" so the file's own CRLF/LF survives the round trip; Python would
        # otherwise translate it away on read and write back the platform default.
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError:
        return False
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()

    start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "[general]"), None)
    if start is None:
        return False
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = i
            break

    cat_at = next((i for i in range(start + 1, end) if _split_key(lines[i]) == "category"), None)
    nex_at = next(
        (i for i in range(start + 1, end) if _split_key(lines[i]) == "nexuscategory"), None
    )
    want_cat, want_nex = meta_ini_lines(mo2_id, nexus_id)

    changed = False
    if cat_at is None:
        lines.insert(end, want_cat)
        end += 1
        cat_at = end - 1
        changed = True
    else:
        current = lines[cat_at].partition("=")[2]
        if lines[cat_at] != want_cat and (force or _is_unset_category(current)):
            lines[cat_at] = want_cat
            changed = True

    if nex_at is None:
        lines.insert(cat_at + 1, want_nex)
        changed = True
    else:
        current = lines[nex_at].partition("=")[2].strip()
        if lines[nex_at] != want_nex and (force or current in ("", "0")):
            lines[nex_at] = want_nex
            changed = True

    if not changed:
        return False
    out = newline.join(lines) + (newline if trailing else "")
    path.write_text(out, encoding="utf-8", newline="")
    return True


# ------------------------------------------------------------------- fetching


def fetch_game_categories(client: NexusClient, domain: str) -> list[GameCategory]:
    """The game's whole category list from the v1 game endpoint (needs the API key)."""
    body = client.game_info(domain)
    raw = body.get("categories") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise NexusError(f"unexpected game info response for {domain}: no categories list")
    out: list[GameCategory] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            nexus_id = int(row["category_id"])
        except (KeyError, TypeError, ValueError):
            continue
        parent = row.get("parent_category")
        out.append(
            GameCategory(
                nexus_id=nexus_id,
                name=str(row.get("name") or ""),
                # `parent_category` is `false` for a top-level category, not null.
                parent_nexus_id=int(parent) if isinstance(parent, int) and parent else None,
            )
        )
    return out


def parse_mod_category_id(value: Any) -> int | None:
    """GraphQL's `modCategory.id` is `"<nexus category id>,<game id>"`; take the first."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    head = value.split(",", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def fetch_mod_categories(
    client: NexusClient, slug: str, revision: int
) -> dict[int, tuple[int, str]]:
    """`modId -> (nexus category id, name)` for every mod file in a collection revision."""
    data = client.graphql(MOD_CATEGORY_QUERY, {"slug": slug, "rev": int(revision)})
    rev = (data or {}).get("collectionRevision") or {}
    out: dict[int, tuple[int, str]] = {}
    for mod_file in rev.get("modFiles") or []:
        file = (mod_file or {}).get("file") or {}
        mod = file.get("mod") or {}
        category = mod.get("modCategory") or {}
        nexus_id = parse_mod_category_id(category.get("id"))
        raw_mod_id = mod.get("modId", file.get("modId"))
        if nexus_id is None or raw_mod_id is None:
            continue
        try:
            mod_id = int(raw_mod_id)
        except (TypeError, ValueError):
            continue
        out.setdefault(mod_id, (nexus_id, str(category.get("name") or "")))
    return out


# --------------------------------------------------------- the per-layer JSON


@dataclass
class LayerCategories:
    """One collection layer's category data, as stored beside its `install.json`."""

    domain: str = ""
    fetched: str = ""
    game_categories: list[dict[str, Any]] = field(default_factory=list)
    mods: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "fetched": self.fetched,
            "game_categories": self.game_categories,
            "mods": self.mods,
        }

    def resolve(self, mod: dict[str, Any], table: CategoryTable) -> tuple[int, int] | None:
        """`(mo2 id, nexus id)` for one manifest mod, or None when nothing matches.

        The GraphQL id wins; the manifest's `details.category` name is the fallback and
        matches only exactly (case- and whitespace-insensitive), because Nexus renames
        categories and a curator's manifest keeps whatever name was current when the
        revision was cut. A known Nexus id the instance's table has no row for still
        returns mo2 id 0, so `nexusCategory` is recorded and MO2's own importer can
        make sense of the mod later.
        """
        nexus_id: int | None = None
        mod_id = (mod.get("source") or {}).get("modId")
        if mod_id is not None:
            record = self.mods.get(str(mod_id))
            if isinstance(record, dict):
                try:
                    nexus_id = int(record["nexus_id"])
                except (KeyError, TypeError, ValueError):
                    nexus_id = None
        if nexus_id is None:
            name = (mod.get("details") or {}).get("category")
            if name:
                nexus_id = table.nexus_id_for_name(str(name))
        if nexus_id is None:
            return None
        return (table.mo2_id_for_nexus(nexus_id) or 0, nexus_id)


def load_layer(path: Path | str) -> LayerCategories | None:
    """Read a `<slug>-<rev>.categories.json`, or None when it is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    mods = data.get("mods")
    return LayerCategories(
        domain=str(data.get("domain") or ""),
        fetched=str(data.get("fetched") or ""),
        game_categories=[r for r in (data.get("game_categories") or []) if isinstance(r, dict)],
        mods={str(k): v for k, v in mods.items() if isinstance(v, dict)}
        if isinstance(mods, dict)
        else {},
    )


def prepare_layer(
    client: NexusClient,
    *,
    domain: str,
    slug: str,
    revision: int,
    manifest: dict[str, Any] | None = None,
    out_json: Path | str,
    instance_dir: Path | str | None = None,
    rep: Reporter | None = None,
) -> LayerCategories | None:
    """Fetch a layer's categories, write `out_json` and seed the instance's .dat files.

    Returns None on any Nexus failure after one warning: an instance without categories
    is a cosmetic loss, never a reason to fail a create/update run.

    The table written to the instance is only a *seed*. Whoever writes `meta.ini` must
    read the ids back with `CategoryTable.from_instance` first, because MO2 or the user
    may already have a different numbering there.
    """
    rep = get_reporter(rep)
    try:
        game_categories = fetch_game_categories(client, domain)
        mod_categories = fetch_mod_categories(client, slug, int(revision))
    except Exception as exc:  # noqa: BLE001 - cosmetic: never let categories end a run
        # Nexus errors, a network blip, a GraphQL shape change, or a client without the
        # v1 endpoint at all: the instance builds the same, just without categories.
        rep.warn(f"categories: could not fetch Nexus categories ({exc}); mods stay uncategorised")
        return None

    table = CategoryTable.from_nexus(game_categories)
    if instance_dir is not None:
        write_instance_files(instance_dir, table, rep)

    layer = LayerCategories(
        domain=domain,
        fetched=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        game_categories=[c.to_json() for c in game_categories],
        mods={str(k): {"nexus_id": v[0], "name": v[1]} for k, v in mod_categories.items()},
    )
    out_path = Path(out_json)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(layer.to_json(), indent=2), encoding="utf-8")
    except OSError as exc:
        rep.warn(f"categories: could not write {out_path} ({exc}); mods stay uncategorised")
        return None

    mods = (manifest or {}).get("mods") or []
    if mods:
        matched = sum(1 for m in mods if layer.resolve(m, table) is not None)
        rep.log(
            f"categories: {len(game_categories)} game categories, "
            f"{matched} of {len(mods)} mod(s) matched"
        )
    return layer
