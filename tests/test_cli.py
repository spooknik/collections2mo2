"""CLI smoke tests: `c2mo2 --help` and every subcommand's `--help` exit 0.

Runs the installed console script via subprocess (no network, no .env access --
argparse handles --help and exits before any command body runs).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import collections2mo2

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
