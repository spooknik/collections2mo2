"""c2mo2 command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    __version__,
    archive_inspect,
    build,
    create,
    downloader,
    installer,
    layers,
    nexus,
    oauth,
    profile,
    survey,
    tools,
    update,
    wabbajack,
)
from .manifest import fetch_manifest, load_manifest, non_nexus_sources, summarise
from .nexus import NOT_SIGNED_IN, AuthRequired, CollectionRef, NexusClient, NexusError


def _client() -> NexusClient:
    return NexusClient(oauth.default_auth())


def cmd_fetch(args: argparse.Namespace) -> int:
    ref = CollectionRef.parse(args.url)
    client = _client()
    try:
        info, path = fetch_manifest(client, ref, args.revision, Path(args.work))
    except AuthRequired as e:
        print(f"error: {e}", file=sys.stderr)
        print(NOT_SIGNED_IN, file=sys.stderr)
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
    versions = summary.get("game_versions") or []
    print(f"target game version: {', '.join(versions) if versions else '(not recorded)'}")
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


# -- sign-in -----------------------------------------------------------------------------

# `api` pulls in the whole engine (and rewrites the tool cache paths for packaged
# builds) at import time, so the sign-in commands import it when they run rather than
# at module import.


def _print_signin(result) -> None:  # api.SignInResult
    membership = "Premium" if result.is_premium else "not Premium"
    suffix = ""
    if isinstance(oauth.default_auth(), nexus.ApiKeyAuth):
        suffix = " [personal API key from NEXUS_API_KEY -- testing only]"
    print(f"Signed in as {result.name} ({membership}).{suffix}")


def cmd_login(args: argparse.Namespace) -> int:
    from . import api

    print("Opening your browser to sign in to Nexus Mods...")
    try:
        result = api.sign_in()
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except api.OperationCancelled as e:
        print(f"Cancelled: {e}", file=sys.stderr)
        return 130
    except api.ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_signin(result)
    if not result.is_premium:
        print(
            "Note: automatic mod downloads need a Nexus Mods Premium account; "
            "without one you would have to download every archive by hand."
        )
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    from . import api

    api.sign_out()
    print("Signed out.")
    print(f"You can also revoke this app's access at {api.nexus_authorized_apps_url()}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    from . import api

    try:
        result = api.check_signin()
    except api.ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_signin(result)
    return 0


def _tolerant_output() -> None:
    """Never let a mod name or a curator's changelog kill a run on an encoding error.

    Redirected output on Windows lands in cp1252, and collection text is full of emoji
    and typographic dashes; `errors="replace"` turns what would be a `UnicodeEncodeError`
    mid-report into a `?`.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _tolerant_output()
    p = argparse.ArgumentParser(
        prog="c2mo2", description="Nexus Mods collections -> Mod Organizer 2 instances"
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"c2mo2 {__version__}",
    )
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

    li = sub.add_parser("login", help="sign in to Nexus Mods in your browser")
    li.set_defaults(func=cmd_login)

    lo = sub.add_parser("logout", help="forget the stored Nexus Mods sign-in")
    lo.set_defaults(func=cmd_logout)

    who = sub.add_parser("whoami", help="show which Nexus Mods account is signed in")
    who.set_defaults(func=cmd_whoami)

    downloader.add_parser(sub)
    archive_inspect.add_parser(sub)
    installer.add_parser(sub)
    profile.add_parser(sub)
    profile.add_instance_parser(sub)
    survey.add_parser(sub)
    build.add_parser(sub)
    create.add_parser(sub)
    layers.add_parser(sub)
    update.add_parser(sub)
    tools.add_parser(sub)
    wabbajack.add_parser(sub)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
