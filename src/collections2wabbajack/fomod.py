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

Deliberate simplifications (all recorded as warnings when they bite):

* ``fileDependency`` cannot be answered -- we have no game directory -- so every
  game file is treated as ``Missing`` and we warn when that actually decided
  something.
* ``gameDependency`` / ``fomodDependency`` (version checks) are always satisfied.
* ``installIfUsable`` is ignored; ``alwaysInstall`` is honoured.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
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


def _eval_deps(deps: Dependencies | None, flags: dict[str, str]) -> tuple[bool, list[str]]:
    """Evaluate a condition tree. Returns (result, influential file dependencies).

    We cannot see the game directory, so every ``fileDependency`` is answered as
    if the file were ``Missing``. The second return value lists the file
    dependencies that actually decided the outcome, so the caller can warn.
    """
    if deps is None or deps.is_empty():
        return True, []

    results: list[tuple[bool, list[str]]] = []
    for name, value in deps.flags:
        results.append((flags.get(name) == value, []))
    for name, state in deps.files:
        ok = state.lower() == "missing"
        results.append((ok, [f"{name} (expected {state}, assumed Missing)"]))
    for child in deps.children:
        results.append(_eval_deps(child, flags))
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


def _resolve_type(plugin: Plugin, flags: dict[str, str], file_deps: list[str]) -> str:
    for deps, name in plugin.type_patterns:
        ok, used = _eval_deps(deps, flags)
        file_deps.extend(used)
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
) -> list[Plugin]:
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
    return _enforce_group_rules(group, selected, types, warnings, where, replay=True)


def _select_default(
    group: Group, types: dict[int, str], warnings: list[str], where: str
) -> list[Plugin]:
    selected = [
        p for p in group.plugins if types[id(p)] in (TYPE_REQUIRED, TYPE_RECOMMENDED)
    ]
    return _enforce_group_rules(group, selected, types, warnings, where, replay=False)


def _enforce_group_rules(
    group: Group,
    selected: list[Plugin],
    types: dict[int, str],
    warnings: list[str],
    where: str,
    replay: bool,
) -> list[Plugin]:
    chosen = [p for p in selected if _selectable(types[id(p)])]
    if len(chosen) != len(selected):
        warnings.append(f"{where}: dropped NotUsable option(s)")

    for plugin in group.plugins:
        if types[id(plugin)] == TYPE_REQUIRED and plugin not in chosen:
            chosen.append(plugin)

    if group.type == "SelectAll":
        chosen = [p for p in group.plugins if _selectable(types[id(p)])]
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
) -> InstallPlan:
    """Run a FOMOD headlessly.

    `fomod_root_listing` is an optional iterable of paths (relative to the FOMOD
    root) used to resolve sources case-insensitively and to warn about sources
    the archive does not actually contain. `choices` is the Vortex
    ``mod["choices"]`` object; when absent, defaults are used.
    """
    config = parse_config(config_xml_path)
    plan = InstallPlan()
    flags: dict[str, str] = {}
    file_deps: list[str] = []
    collected: list[tuple[int, int, FileSpec]] = []
    seq = 0

    for spec in config.required_files:
        collected.append((spec.priority, seq, spec))
        seq += 1

    recorded = _recorded_steps(choices)
    replay = bool(recorded)
    matched = _match_recorded(config.steps, recorded, plan.warnings) if replay else []

    for index, step in enumerate(config.steps):
        visible, used = _eval_deps(step.visible, flags)
        file_deps.extend(used)
        if not visible:
            continue
        recorded_step = matched[index] if replay and index < len(matched) else None
        for gi, group in enumerate(step.groups):
            where = f"step {step.name!r} group {group.name!r}"
            if group.type not in GROUP_TYPES:
                plan.warnings.append(f"{where}: unknown group type {group.type!r}, treated as SelectAny")
            types = {id(p): _resolve_type(p, flags, file_deps) for p in group.plugins}
            if replay:
                chosen = _select_replay(group, _recorded_group(group, gi, recorded_step), types, plan.warnings, where)
            else:
                chosen = _select_default(group, types, plan.warnings, where)
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
        ok, used = _eval_deps(deps, flags)
        file_deps.extend(used)
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

    for dep in dict.fromkeys(file_deps):
        plan.warnings.append(f"fileDependency assumed Missing (no game directory): {dep}")
    return plan
