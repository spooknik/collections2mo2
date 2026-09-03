"""Fetch a collection archive, extract collection.json, and summarise it."""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import py7zr

from .nexus import CollectionRef, NexusClient, RevisionInfo

MANIFEST_NAME = "collection.json"
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def _extract_archive(archive: Path, dest: Path) -> Path:
    """Extract the whole collection archive (7z or zip) into `dest`; return the manifest path.

    The archive also carries bundled files and binary patches, so we keep all of it.
    """
    with open(archive, "rb") as fh:
        head = fh.read(8)
    dest.mkdir(parents=True, exist_ok=True)
    if head.startswith(SEVENZIP_MAGIC):
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(path=dest)
    elif head.startswith(b"PK"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    elif head.lstrip().startswith(b"{"):
        # Some responses may just be the JSON itself
        (dest / MANIFEST_NAME).write_bytes(archive.read_bytes())
    else:
        raise ValueError(f"unrecognised archive format for {archive.name}: {head!r}")

    candidates = [p for p in dest.rglob(MANIFEST_NAME) if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"{MANIFEST_NAME} not in {archive.name}")
    return min(candidates, key=lambda p: len(p.parts))


def fetch_manifest(
    client: NexusClient,
    ref: CollectionRef,
    revision: int | None,
    work_dir: Path,
    info: RevisionInfo | None = None,
) -> tuple[RevisionInfo, Path]:
    """Download the collection archive for a revision and extract collection.json.

    Returns (revision info, path to extracted collection.json). The archive is kept
    next to it so bundled files / patches can be read later. Pass `info` when the
    caller has already resolved the revision, to save a second GraphQL round trip.
    """
    if info is None:
        info = client.revision_info(ref, revision)
    out_dir = work_dir / ref.slug / str(info.revision_number)
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted = out_dir / "archive"
    existing = [p for p in extracted.rglob(MANIFEST_NAME)] if extracted.exists() else []
    if existing:
        return info, min(existing, key=lambda p: len(p.parts))

    archive = out_dir / "collection_archive.bin"
    if not archive.exists():
        url = client.collection_download_url(info)
        client.download(url, archive)
    return info, _extract_archive(archive, extracted)


# -- Reporting ----------------------------------------------------------------


def install_mode(mod: dict[str, Any]) -> str:
    """Classify how Vortex expects this mod to be installed.

    replicate: curator exported the installed file list (+patches) -> deterministic
    choices:   FOMOD installer with the curator's recorded answers
    fresh:     plain install; if the archive has a FOMOD it would run interactively
    """
    if mod.get("hashes"):
        return "replicate"
    if mod.get("choices"):
        return "choices"
    return "fresh"


def summarise(manifest: dict[str, Any]) -> dict[str, Any]:
    mods: list[dict[str, Any]] = manifest.get("mods") or []
    info = manifest.get("info") or {}
    rules = manifest.get("modRules") or []

    by_source = Counter((m.get("source") or {}).get("type", "?") for m in mods)
    by_mode = Counter(install_mode(m) for m in mods)
    by_policy = Counter((m.get("source") or {}).get("updatePolicy", "?") for m in mods)
    optional = sum(1 for m in mods if m.get("optional"))
    with_patches = [m for m in mods if m.get("patches")]
    patch_files = sum(len(m["patches"]) for m in with_patches)
    with_overrides = sum(1 for m in mods if m.get("fileOverrides"))
    with_instructions = sum(1 for m in mods if (m.get("instructions") or "").strip())
    phases = Counter(m.get("phase", 0) for m in mods)
    rule_types = Counter(r.get("type", "?") for r in rules)
    mod_types = Counter((m.get("details") or {}).get("type") or "" for m in mods)
    replicate_files = sum(len(m.get("hashes") or []) for m in mods)

    extra_keys = {
        k: (len(v) if isinstance(v, (list, dict)) else v)
        for k, v in manifest.items()
        if k not in ("info", "mods", "modRules")
    }

    return {
        "name": info.get("name"),
        "author": info.get("author"),
        "game": info.get("domainName"),
        "game_versions": [str(v) for v in (info.get("gameVersions") or [])],
        "mods": len(mods),
        "optional": optional,
        "by_source": dict(by_source),
        "by_install_mode": dict(by_mode),
        "by_update_policy": dict(by_policy),
        "replicate_file_entries": replicate_files,
        "mods_with_patches": len(with_patches),
        "patch_files": patch_files,
        "mods_with_file_overrides": with_overrides,
        "mods_with_instructions": with_instructions,
        "mod_types": dict(mod_types),
        "phases": dict(sorted(phases.items())),
        "mod_rules": len(rules),
        "mod_rule_types": dict(rule_types),
        "other_top_level_keys": extra_keys,
    }


def non_nexus_sources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for m in manifest.get("mods") or []:
        src = m.get("source") or {}
        if src.get("type") != "nexus":
            out.append(
                {
                    "name": m.get("name"),
                    "type": src.get("type"),
                    "url": src.get("url"),
                    "instructions": (src.get("instructions") or m.get("instructions") or "")[:120],
                }
            )
    return out


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
