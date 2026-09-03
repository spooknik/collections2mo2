"""Shared pytest configuration for collections2mo2.

No network access, no real archives (except the ``local`` marked test, which is
skipped when tools/7za.exe has not been bootstrapped -- see README/docs/architecture.md).
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
