"""c2wj command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import archive_inspect, downloader, installer, profile, survey
from .manifest import fetch_manifest, load_manifest, non_nexus_sources, summarise
from .nexus import AuthRequired, CollectionRef, NexusClient, NexusError


def _client() -> NexusClient:
    load_dotenv()
    key = os.environ.get("NEXUS_API_KEY") or None
    return NexusClient(api_key=key)


def cmd_fetch(args: argparse.Namespace) -> int:
    ref = CollectionRef.parse(args.url)
    client = _client()
    try:
        info, path = fetch_manifest(client, ref, args.revision, Path(args.work))
    except AuthRequired as e:
        print(f"error: {e}", file=sys.stderr)
        print(
            "Put a personal API key in .env as NEXUS_API_KEY= (see .env.example).",
            file=sys.stderr,
        )
        return 2
    except NexusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{info.name} [{info.game}] revision {info.revision_number}")
    print(f"  mods: {info.mod_count}  total size: {info.total_size / 1e9:.2f} GB")
    print(f"  manifest: {path}")
    if args.report:
        _print_report(path, args.json)
    return 0


def _print_report(path: Path, as_json: bool) -> None:
    manifest = load_manifest(path)
    summary = summarise(manifest)
    if as_json:
        print(json.dumps(summary, indent=2))
        return
    print(f"\n== {summary['name']} by {summary['author']} ({summary['game']})")
    print(f"mods: {summary['mods']}  (optional: {summary['optional']})")
    print(f"sources:        {summary['by_source']}")
    print(f"install modes:  {summary['by_install_mode']}")
    print(f"update policy:  {summary['by_update_policy']}")
    print(f"replicate file entries: {summary['replicate_file_entries']}")
    print(
        f"patches: {summary['patch_files']} files across {summary['mods_with_patches']} mods; "
        f"fileOverrides on {summary['mods_with_file_overrides']} mods; "
        f"instructions on {summary['mods_with_instructions']} mods"
    )
    print(f"mod types (details.type): {summary['mod_types']}")
    print(f"phases: {summary['phases']}")
    print(f"mod rules: {summary['mod_rules']} {summary['mod_rule_types']}")
    print(f"other top-level keys: {summary['other_top_level_keys']}")
    extra = non_nexus_sources(manifest)
    if extra:
        print(f"\nnon-Nexus sources ({len(extra)}):")
        for e in extra:
            print(f"  - [{e['type']}] {e['name']}  {e['url'] or ''}  {e['instructions']}")


def cmd_report(args: argparse.Namespace) -> int:
    _print_report(Path(args.manifest), args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="c2wj", description="Nexus collections -> MO2 / Wabbajack")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download a collection revision's manifest")
    f.add_argument("url", help="collection URL")
    f.add_argument("--revision", type=int, default=None, help="revision number (default: latest)")
    f.add_argument("--work", default="work", help="work directory (default: ./work)")
    f.add_argument("--report", action="store_true", help="print a summary after fetching")
    f.add_argument("--json", action="store_true", help="summary as JSON")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("report", help="summarise an already-fetched collection.json")
    r.add_argument("manifest", help="path to collection.json")
    r.add_argument("--json", action="store_true", help="summary as JSON")
    r.set_defaults(func=cmd_report)

    downloader.add_parser(sub)
    archive_inspect.add_parser(sub)
    installer.add_parser(sub)
    profile.add_parser(sub)
    survey.add_parser(sub)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
