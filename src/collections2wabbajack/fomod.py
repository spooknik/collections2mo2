"""Parse and evaluate FOMOD installers (``fomod/ModuleConfig.xml``).

Two use cases:

* **Choice replay** -- the Vortex collection manifest records the answers the
  curator gave to the installer wizard (``mod["choices"]``). We re-run the
  wizard headlessly and replay those answers to get the exact same file set.
* **Defaults** -- for mods whose manifest entry has no recorded choices (they
  were installed before the FOMOD existed, or the curator kept the defaults),
  we pick Required + Recommended options and warn loudly about what we chose.

Everything here is a pure function over the XML plus a directory listing, so it
is unit-testable without extracting anything: :func:`evaluate` returns an
:class:`InstallPlan` of ``(source, destination)`` pairs, and the caller copies.

Two things decide what a ``<plugin>`` ends up as:

* **The recorded choices win.** A plugin the curator explicitly picked is
  installed whatever its ``typeDescriptor`` resolves to. The curator's Vortex
  could see the game; we largely cannot, so a locally-computed ``NotUsable`` is
  evidence about *our* knowledge, not about the pick. Only a recorded option
  that is literally absent from the XML is dropped (with a warning). The
  "Required always / NotUsable never" rules still govern everything that was
  *not* recorded (defaults mode, and groups the curator never answered).
* **``fileDependency`` is answered by a resolver**, not assumed. Callers pass
  ``file_state``: ``(filename) -> "Active" | "Inactive" | "Missing"``. The
  installer builds one from the manifest's load order, the game's ``Data``
  folder and the files earlier mods in the batch already installed. With no
  resolver every file is ``Missing``, which is the old behaviour.

Deliberate simplifications (all recorded as warnings when they bite):

* ``gameDependency`` / ``fomodDependency`` (version checks) are always satisfied.
* ``installIfUsable`` is ignored; ``alwaysInstall`` is honoured.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Plugin types, in the FOMOD sense. Required is force-selected, NotUsable is
# force-deselected, Recommended is the default pick, the rest are free choices.
TYPE_REQUIRED = "Required"
TYPE_RECOMMENDED = "Recommended"
TYPE_OPTIONAL = "Optional"
TYPE_COULD_BE_USABLE = "CouldBeUsable"
TYPE_NOT_USABLE = "NotUsable"

# The three states a <fileDependency> can ask about, and the resolver signature.
STATE_ACTIVE = "Active"
STATE_INACTIVE = "Inactive"
STATE_MISSING = "Missing"

#: ``(filename) -> "Active" | "Inactive" | "Missing"``. ``Missing`` doubles as
#: "we have no idea", which is why it is also the answer when no resolver is given.
FileState = Callable[[str], str]

GROUP_TYPES = {
    "SelectExactlyOne",
    "SelectAtMostOne",
    "SelectAtLeastOne",
    "SelectAny",
    "SelectAll",
}

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------- model


@dataclass
class FileSpec:
    """One ``<file>`` or ``<folder>`` entry inside a ``<files>`` block."""

    is_folder: bool
    source: str
    destination: str | None
    priority: int = 0
    always_install: bool = False
    install_if_usable: bool = False


@dataclass
class Dependencies:
    """A ``<dependencies>`` / ``<visible>`` / ``<pattern>`` condition tree."""

    operator: str = "And"
    flags: list[tuple[str, str]] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    children: list[Dependencies] = field(default_factory=list)
    # gameDependency / fomodDependency, kept only so an "empty" tree is still empty
    ignored: int = 0

    def is_empty(self) -> bool:
        return not (self.flags or self.files or self.children or self.ignored)


@dataclass
class Plugin:
    name: str
    files: list[FileSpec] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    default_type: str = TYPE_OPTIONAL
    type_patterns: list[tuple[Dependencies, str]] = field(default_factory=list)


@dataclass
class Group:
    name: str
    type: str = "SelectAny"
    plugins: list[Plugin] = field(default_factory=list)


@dataclass
class Step:
    name: str
    visible: Dependencies | None = None
    groups: list[Group] = field(default_factory=list)


@dataclass
class Config:
    module_name: str = ""
    required_files: list[FileSpec] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    conditional: list[tuple[Dependencies, list[FileSpec]]] = field(default_factory=list)


@dataclass
class InstallPlan:
    """Result of evaluating a FOMOD.

    ``files`` are ``(source, destination)`` pairs relative to the FOMOD root
    (the directory that *contains* the ``fomod`` folder) and the mod folder
    respectively. A source may be a folder; the caller expands it.
    """

    files: list[tuple[str, str]] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    selections: list[tuple[str, str, list[str]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``fileDependency`` checks the resolver could answer (Active/Inactive) ...
    resolved_deps: int = 0
    #: ... and the ones it could not, which fall back to ``Missing``.
    unknown_deps: int = 0


# --------------------------------------------------------------------------- parsing


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    text = text.lstrip("﻿")
    # We hand a str to ElementTree, so any encoding declaration must go.
    text = _XML_DECL.sub("", text, count=1)
    return _CONTROL.sub("", text)


def _tag(el: ET.Element) -> str:
    tag = el.tag
    if isinstance(tag, str):
        return tag.rsplit("}", 1)[-1].lower()
    return ""


def _attr(el: ET.Element, name: str, default: str = "") -> str:
    name = name.lower()
    for key, value in el.attrib.items():
        if key.rsplit("}", 1)[-1].lower() == name:
            return value
    return default


def _kids(el: ET.Element | None, name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if _tag(c) == name]


def _kid(el: ET.Element | None, name: str) -> ET.Element | None:
    kids = _kids(el, name)
    return kids[0] if kids else None


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().strip("/")


def _ordered(container: ET.Element | None, names: Iterable[str]) -> list[ET.Element]:
    """Children of `container` with one of `names`, honouring its ``order`` attribute."""
    if container is None:
        return []
    wanted = set(names)
    items = [c for c in container if _tag(c) in wanted]
    order = (_attr(container, "order", "Ascending") or "Ascending").lower()
    if order == "explicit":
        return items
    items = sorted(items, key=lambda e: _attr(e, "name", "").lower())
    if order == "descending":
        items.reverse()
    return items


def _parse_files(el: ET.Element | None) -> list[FileSpec]:
    specs: list[FileSpec] = []
    if el is None:
        return specs
    for child in el:
        if _tag(child) not in ("file", "folder"):
            continue
        source = _norm(_attr(child, "source"))
        if not source:
            continue
        dest_raw = _attr(child, "destination", "\x00")
        destination = None if dest_raw == "\x00" else _norm(dest_raw)
        try:
            priority = int(_attr(child, "priority", "0") or 0)
        except ValueError:
            priority = 0
        specs.append(
            FileSpec(
                is_folder=_tag(child) == "folder",
                source=source,
                destination=destination,
                priority=priority,
                always_install=_attr(child, "alwaysinstall", "false").lower() == "true",
                install_if_usable=_attr(child, "installifusable", "false").lower() == "true",
            )
        )
    return specs


def _parse_deps(el: ET.Element | None) -> Dependencies:
    """Parse a ``<dependencies>``-shaped element (also ``<visible>``, ``<pattern>``)."""
    if el is None:
        return Dependencies()
    deps = Dependencies(operator=(_attr(el, "operator", "And") or "And").capitalize())
    for child in el:
        tag = _tag(child)
        if tag == "flagdependency":
            deps.flags.append((_attr(child, "flag"), _attr(child, "value")))
        elif tag == "filedependency":
            deps.files.append((_norm(_attr(child, "file")), _attr(child, "state", "Active")))
        elif tag == "dependencies":
            deps.children.append(_parse_deps(child))
        elif tag in ("gamedependency", "fommdependency", "fomoddependency"):
            deps.ignored += 1
    return deps


def _parse_type_descriptor(el: ET.Element | None) -> tuple[str, list[tuple[Dependencies, str]]]:
    simple = _kid(el, "type")
    if simple is not None:
        return _attr(simple, "name", TYPE_OPTIONAL) or TYPE_OPTIONAL, []
    dep_type = _kid(el, "dependencytype")
    if dep_type is None:
        return TYPE_OPTIONAL, []
    default = _attr(_kid(dep_type, "defaulttype"), "name", TYPE_OPTIONAL) or TYPE_OPTIONAL
    patterns: list[tuple[Dependencies, str]] = []
    for pattern in _kids(_kid(dep_type, "patterns"), "pattern"):
        deps_el = _kid(pattern, "dependencies") or pattern
        name = _attr(_kid(pattern, "type"), "name", TYPE_OPTIONAL) or TYPE_OPTIONAL
        patterns.append((_parse_deps(deps_el), name))
    return default, patterns


def parse_config(config_xml_path: Path | str) -> Config:
    """Parse a ModuleConfig.xml into a :class:`Config` (tolerates BOMs and namespaces)."""
    raw = Path(config_xml_path).read_bytes()
    root = ET.fromstring(_decode(raw))
    name_el = _kid(root, "modulename")
    config = Config(module_name=(name_el.text or "").strip() if name_el is not None else "")
    config.required_files = _parse_files(_kid(root, "requiredinstallfiles"))

    for step_el in _ordered(_kid(root, "installsteps"), ("installstep",)):
        step = Step(name=_attr(step_el, "name"))
        visible_el = _kid(step_el, "visible")
        if visible_el is not None:
            # <visible> may hold the conditions directly or wrap a <dependencies>.
            inner = _kid(visible_el, "dependencies")
            step.visible = _parse_deps(inner if inner is not None and len(visible_el) == 1 else visible_el)
        for group_el in _ordered(_kid(step_el, "optionalfilegroups"), ("group",)):
            gtype = _attr(group_el, "type", "SelectAny") or "SelectAny"
            group = Group(name=_attr(group_el, "name"), type=gtype)
            for plugin_el in _ordered(_kid(group_el, "plugins"), ("plugin",)):
                plugin = Plugin(name=_attr(plugin_el, "name"))
                plugin.files = _parse_files(_kid(plugin_el, "files"))
                for flag_el in _kids(_kid(plugin_el, "conditionflags"), "flag"):
                    plugin.flags.append((_attr(flag_el, "name"), (flag_el.text or "").strip()))
                plugin.default_type, plugin.type_patterns = _parse_type_descriptor(
                    _kid(plugin_el, "typedescriptor")
                )
                group.plugins.append(plugin)
            step.groups.append(group)
        config.steps.append(step)

    for pattern in _kids(_kid(_kid(root, "conditionalfileinstalls"), "patterns"), "pattern"):
        deps_el = _kid(pattern, "dependencies") or pattern
        config.conditional.append((_parse_deps(deps_el), _parse_files(_kid(pattern, "files"))))
    return config


# ----------------------------------------------------------------------- evaluation


class _DepContext:
    """Answers ``fileDependency`` questions and counts what it could answer.

    ``Missing`` is both a real FOMOD state and our "no idea" answer, so the
    counters split the two: ``resolved`` is Active/Inactive (we know), ``unknown``
    is everything the resolver could not place.
    """

    __slots__ = ("_file_state", "resolved", "unknown")

    def __init__(self, file_state: FileState | None) -> None:
        self._file_state = file_state
        self.resolved = 0
        self.unknown = 0

    def state(self, filename: str) -> str:
        value = ""
        if self._file_state is not None:
            try:
                value = (self._file_state(filename) or "").strip().capitalize()
            except Exception:  # noqa: BLE001 - a bad resolver must not kill the install
                value = ""
        if value in (STATE_ACTIVE, STATE_INACTIVE):
            self.resolved += 1
            return value
        self.unknown += 1
        return STATE_MISSING


def _eval_deps(
    deps: Dependencies | None, flags: dict[str, str], ctx: _DepContext
) -> tuple[bool, list[str]]:
    """Evaluate a condition tree. Returns (result, influential *unknown* file deps).

    The second return value lists only the file dependencies the resolver could
    not answer *and* that decided the outcome, so the caller can warn about the
    ones where our guess may be wrong.
    """
    if deps is None or deps.is_empty():
        return True, []

    results: list[tuple[bool, list[str]]] = []
    for name, value in deps.flags:
        results.append((flags.get(name) == value, []))
    for name, wanted in deps.files:
        actual = ctx.state(name)
        ok = actual.lower() == (wanted or STATE_ACTIVE).strip().lower()
        blind = [f"{name} (wanted {wanted}, unknown so assumed Missing)"]
        results.append((ok, blind if actual == STATE_MISSING else []))
    for child in deps.children:
        results.append(_eval_deps(child, flags, ctx))
    for _ in range(deps.ignored):
        results.append((True, []))

    if deps.operator.lower() == "or":
        value = any(r for r, _ in results)
        # Only the branches that made it true (or, if false, all of them) mattered.
        influential = [f for r, fs in results for f in fs if (r if value else True)]
    else:
        value = all(r for r, _ in results)
        influential = [f for r, fs in results for f in fs if (True if value else not r)]
    return value, influential


def _resolve_type(
    plugin: Plugin, flags: dict[str, str], ctx: _DepContext, blind: list[str]
) -> str:
    for deps, name in plugin.type_patterns:
        ok, used = _eval_deps(deps, flags, ctx)
        blind.extend(used)
        if ok:
            return name
    return plugin.default_type


def _selectable(type_name: str) -> bool:
    return type_name != TYPE_NOT_USABLE


def _recorded_steps(choices: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(choices, dict):
        return []
    options = choices.get("options")
    return [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []


def _recorded_answered(recorded_step: dict[str, Any] | None) -> bool:
    """Did the curator actually tick anything in this step?

    Vortex writes an entry for every step, including ones it never showed; those
    come through with every group's ``choices`` empty. A non-empty one is proof
    the step *was* visible to the curator.
    """
    groups = (recorded_step or {}).get("groups")
    if not isinstance(groups, list):
        return False
    return any(
        isinstance(g, dict) and isinstance(g.get("choices"), list) and g["choices"]
        for g in groups
    )


def _match_recorded(
    steps: list[Step], recorded: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any] | None]:
    """Line up recorded wizard answers with the XML install steps.

    Vortex records one entry per install step in XML order -- *including* steps
    that were never shown (those come through with empty ``choices``) -- so the
    mapping is positional. If the counts disagree we fall back to matching by
    step name, in order.
    """
    if len(recorded) == len(steps):
        for step, rec in zip(steps, recorded, strict=True):
            if (rec.get("name") or "") != step.name:
                warnings.append(
                    f"recorded step name {rec.get('name')!r} != XML step {step.name!r} "
                    "(kept positional mapping)"
                )
        return list(recorded)

    warnings.append(
        f"recorded {len(recorded)} wizard steps but the FOMOD has {len(steps)}; matching by name"
    )
    pool = list(recorded)
    matched: list[dict[str, Any] | None] = []
    for step in steps:
        hit = next((r for r in pool if (r.get("name") or "") == step.name), None)
        if hit is not None:
            pool.remove(hit)
        matched.append(hit)
    return matched


def _recorded_group(
    group: Group, index: int, recorded_step: dict[str, Any] | None
) -> dict[str, Any] | None:
    groups = (recorded_step or {}).get("groups")
    if not isinstance(groups, list):
        return None
    groups = [g for g in groups if isinstance(g, dict)]
    hit = next((g for g in groups if (g.get("name") or "") == group.name), None)
    if hit is None and index < len(groups):
        hit = groups[index]
    return hit


def _select_replay(
    group: Group,
    recorded_group: dict[str, Any] | None,
    types: dict[int, str],
    warnings: list[str],
    where: str,
) -> tuple[list[Plugin], list[Plugin]]:
    """Replay one group. Returns (what the curator recorded, what we install)."""
    selected: list[Plugin] = []
    raw = (recorded_group or {}).get("choices")
    entries = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
    for entry in entries:
        name = entry.get("name")
        hit = next((p for p in group.plugins if p.name == name), None)
        if hit is None:
            idx = entry.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(group.plugins):
                hit = group.plugins[idx]
                warnings.append(f"{where}: recorded option {name!r} not found, used index {idx}")
            else:
                warnings.append(f"{where}: recorded option {name!r} not found, ignored")
                continue
        if hit not in selected:
            selected.append(hit)
    chosen = _enforce_group_rules(group, selected, types, warnings, where, recorded=selected)
    return selected, chosen


def _select_default(
    group: Group, types: dict[int, str], warnings: list[str], where: str
) -> list[Plugin]:
    selected = [
        p for p in group.plugins if types[id(p)] in (TYPE_REQUIRED, TYPE_RECOMMENDED)
    ]
    return _enforce_group_rules(group, selected, types, warnings, where, recorded=None)


def _enforce_group_rules(
    group: Group,
    selected: list[Plugin],
    types: dict[int, str],
    warnings: list[str],
    where: str,
    recorded: list[Plugin] | None,
) -> list[Plugin]:
    """Apply the group's own rules to a proposed selection.

    ``recorded`` is the curator's explicit picks (``None`` in defaults mode).
    Those are immune to ``NotUsable``: the curator's installer could see the
    game, so a ``NotUsable`` we computed without that knowledge is not a reason
    to overrule them.
    """
    replay = recorded is not None
    pinned = {id(p) for p in recorded or ()}

    def keep(plugin: Plugin) -> bool:
        return _selectable(types[id(plugin)]) or id(plugin) in pinned

    chosen = [p for p in selected if keep(p)]
    if len(chosen) != len(selected):
        warnings.append(f"{where}: dropped NotUsable option(s)")

    for plugin in group.plugins:
        if types[id(plugin)] == TYPE_REQUIRED and plugin not in chosen:
            chosen.append(plugin)

    if group.type == "SelectAll":
        chosen = [p for p in group.plugins if keep(p)]
    elif group.type in ("SelectExactlyOne", "SelectAtLeastOne") and not chosen:
        fallback = next(
            (p for p in group.plugins if types[id(p)] == TYPE_RECOMMENDED),
            next((p for p in group.plugins if _selectable(types[id(p)])), None),
        )
        if fallback is not None:
            chosen = [fallback]
            warnings.append(
                f"{where}: {group.type} with nothing "
                f"{'recorded' if replay else 'recommended'}; picked {fallback.name!r}"
            )
    elif group.type in ("SelectExactlyOne", "SelectAtMostOne") and len(chosen) > 1:
        warnings.append(
            f"{where}: {group.type} with {len(chosen)} selections; kept {chosen[0].name!r}"
        )
        chosen = chosen[:1]

    # Keep the group's own plugin order so file collection is deterministic.
    order = {id(p): i for i, p in enumerate(group.plugins)}
    return sorted(chosen, key=lambda p: order.get(id(p), 0))


def _listing_index(listing: Iterable[str] | None) -> dict[str, str] | None:
    if listing is None:
        return None
    index: dict[str, str] = {}
    for raw in listing:
        path = _norm(str(raw))
        if not path:
            continue
        index.setdefault(path.lower(), path)
        # Register parent directories too, so folder sources resolve.
        parts = path.split("/")
        for i in range(1, len(parts)):
            index.setdefault("/".join(parts[:i]).lower(), "/".join(parts[:i]))
    return index


def evaluate(
    config_xml_path: Path | str,
    fomod_root_listing: Iterable[str] | None = None,
    choices: dict[str, Any] | None = None,
    file_state: FileState | None = None,
) -> InstallPlan:
    """Run a FOMOD headlessly.

    `fomod_root_listing` is an optional iterable of paths (relative to the FOMOD
    root) used to resolve sources case-insensitively and to warn about sources
    the archive does not actually contain. `choices` is the Vortex
    ``mod["choices"]`` object; when absent, defaults are used. `file_state`
    answers ``fileDependency`` questions -- see :data:`FileState`; without one
    every file reads as ``Missing``.
    """
    config = parse_config(config_xml_path)
    plan = InstallPlan()
    flags: dict[str, str] = {}
    ctx = _DepContext(file_state)
    # Unknown file dependencies that actually decided something, plus the places
    # where our answer disagreed with what the curator recorded.
    blind: list[str] = []
    diverged: list[str] = []
    collected: list[tuple[int, int, FileSpec]] = []
    seq = 0

    for spec in config.required_files:
        collected.append((spec.priority, seq, spec))
        seq += 1

    recorded = _recorded_steps(choices)
    replay = bool(recorded)
    matched = _match_recorded(config.steps, recorded, plan.warnings) if replay else []

    for index, step in enumerate(config.steps):
        recorded_step = matched[index] if replay and index < len(matched) else None
        visible, used = _eval_deps(step.visible, flags, ctx)
        blind.extend(used)
        if not visible and _recorded_answered(recorded_step):
            # The curator ticked boxes here, so the step was on screen for them;
            # our <visible> verdict is the thing that is wrong.
            visible = True
            diverged.append(
                f"step {step.name!r}: evaluated as hidden but the curator recorded "
                "choices in it; treated as visible"
            )
        if not visible:
            continue
        for gi, group in enumerate(step.groups):
            where = f"step {step.name!r} group {group.name!r}"
            if group.type not in GROUP_TYPES:
                plan.warnings.append(
                    f"{where}: unknown group type {group.type!r}, treated as SelectAny"
                )
            group_blind: list[str] = []
            types = {id(p): _resolve_type(p, flags, ctx, group_blind) for p in group.plugins}
            if replay:
                picked, chosen = _select_replay(
                    group, _recorded_group(group, gi, recorded_step), types, plan.warnings, where
                )
                if group_blind and {p.name for p in chosen} != {p.name for p in picked}:
                    added = sorted({p.name for p in chosen} - {p.name for p in picked})
                    dropped = sorted({p.name for p in picked} - {p.name for p in chosen})
                    diverged.append(
                        f"{where}: unresolved fileDependency changed the selection "
                        f"(added {added or 'nothing'}, dropped {dropped or 'nothing'})"
                    )
                    blind.extend(group_blind)
            else:
                chosen = _select_default(group, types, plan.warnings, where)
                blind.extend(group_blind)
                plan.warnings.append(
                    f"default choice -- {where} [{group.type}]: "
                    + (", ".join(p.name for p in chosen) or "(nothing)")
                )
            plan.selections.append((step.name, group.name, [p.name for p in chosen]))
            for plugin in group.plugins:
                if plugin in chosen:
                    for name, value in plugin.flags:
                        flags[name] = value
                    for spec in plugin.files:
                        collected.append((spec.priority, seq, spec))
                        seq += 1
                else:
                    for spec in plugin.files:
                        if spec.always_install:
                            collected.append((spec.priority, seq, spec))
                            seq += 1

    for deps, specs in config.conditional:
        ok, used = _eval_deps(deps, flags, ctx)
        blind.extend(used)
        if ok:
            for spec in specs:
                collected.append((spec.priority, seq, spec))
                seq += 1

    plan.flags = flags
    index = _listing_index(fomod_root_listing)
    for _priority, _seq, spec in sorted(collected, key=lambda item: (item[0], item[1])):
        source = spec.source
        if index is not None:
            actual = index.get(source.lower())
            if actual is None:
                plan.warnings.append(f"FOMOD source not in archive: {source}")
                continue
            source = actual
        destination = spec.destination
        if destination is None:
            destination = "" if spec.is_folder else source.rsplit("/", 1)[-1]
        plan.files.append((source, destination))

    plan.resolved_deps = ctx.resolved
    plan.unknown_deps = ctx.unknown
    plan.warnings.extend(diverged)
    if diverged or blind:
        # One summary line per mod, not one per check: the detail lives in the
        # counters, which the installer records in install.json.
        plan.warnings.append(
            f"{ctx.resolved} fileDependency checks resolved via manifest/game/installed; "
            f"{ctx.unknown} still unknown"
        )
    return plan
