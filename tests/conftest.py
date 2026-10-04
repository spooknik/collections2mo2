"""Shared pytest configuration for collections2mo2.

No network access, no real archives (except the ``local`` marked test, which is
skipped when tools/7za.exe has not been bootstrapped -- see README/docs/architecture.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from collections2mo2 import oauth

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeAuth(requests.auth.AuthBase):
    """A stand-in for the OAuth sign-in: a `NexusClient` built with it counts as signed
    in, and it attaches nothing to the requests it sees."""

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        return r


@pytest.fixture
def signed_in(monkeypatch) -> FakeAuth:
    """Make `oauth.default_auth()` return a `FakeAuth`, as if the user had signed in,
    without touching the real credential store."""
    auth = FakeAuth()
    monkeypatch.setattr(oauth, "_cached_auth", None)
    monkeypatch.setattr(oauth, "default_auth", lambda: auth)
    return auth
