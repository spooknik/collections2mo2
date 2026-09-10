"""CLI smoke tests: `c2mo2 --help` and every subcommand's `--help` exit 0.

Runs the installed console script via subprocess (no network, no .env access --
argparse handles --help and exits before any command body runs).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

import collections2mo2
from collections2mo2.nexus import ApiKeyAuth, NexusClient

SUBCOMMANDS = [
    "fetch",
    "report",
    "download",
    "inspect",
    "install",
    "profile",
    "survey",
    "build",
    "create",
    "add",
    "remove",
    "update",
    "status",
    "login",
    "logout",
    "whoami",
]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: PLW1510 - --help always exits 0/2, we assert on it
        [sys.executable, "-m", "collections2mo2.cli", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_help_exits_zero():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "c2mo2" in result.stdout


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_subcommand_help_exits_zero(subcommand: str):
    result = _run([subcommand, "--help"])
    assert result.returncode == 0, result.stderr
    assert subcommand in result.stdout.lower() or "usage" in result.stdout.lower()


def test_cli_no_args_fails_with_usage_error():
    result = _run([])
    assert result.returncode != 0


def test_cli_version_exits_zero():
    result = _run(["--version"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"c2mo2 {collections2mo2.__version__}"


def test_create_help_lists_tools_option():
    result = _run(["create", "--help"])
    assert result.returncode == 0
    assert "--tools" in result.stdout


def test_help_lists_the_sign_in_commands():
    result = _run(["--help"])
    assert result.returncode == 0
    for name in ("login", "logout", "whoami"):
        assert name in result.stdout


def test_whoami_without_a_sign_in_reports_it(tmp_path):
    """Run from an empty cwd so a developer's own `.env` cannot sign the test in, with
    the credential store faked empty."""
    env = {
        **os.environ,
        "NEXUS_API_KEY": "",
        "PYTHONPATH": str(Path(collections2mo2.__file__).resolve().parents[2]),
    }
    code = (
        "import keyring, sys;"
        "keyring.get_password = lambda *a, **kw: None;"
        "from collections2mo2.cli import main;"
        "sys.exit(main(['whoami']))"
    )
    result = subprocess.run(  # noqa: PLW1510 - the return code is the assertion
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not signed in to Nexus Mods" in result.stderr


# -- identifying headers -------------------------------------------------------------------


def test_nexus_client_sends_the_identifying_headers():
    client = NexusClient()
    assert client.session.headers["Application-Name"] == "collections2mo2"
    assert client.session.headers["Application-Version"] == collections2mo2.__version__
    assert collections2mo2.__version__ in client.session.headers["User-Agent"]
    assert client.authenticated is False


def test_nexus_client_takes_an_api_key_shorthand():
    client = NexusClient(api_key="dev-key")
    assert isinstance(client.auth, ApiKeyAuth)
    assert client.session.auth is client.auth
    assert client.authenticated is True


def test_api_key_auth_signs_only_the_api_host():
    auth = ApiKeyAuth("dev-key")
    signed = auth(requests.Request("GET", "https://api.nexusmods.com/v1/games/x.json").prepare())
    assert signed.headers["apikey"] == "dev-key"
    cdn = auth(requests.Request("GET", "https://cdn.nexusmods.com/file.7z").prepare())
    assert "apikey" not in cdn.headers
