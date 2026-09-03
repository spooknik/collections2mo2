"""Tests for archive_inspect.inspect_archive().

The real inspector shells out to tools/7za.exe (see sevenzip.py's module docstring
for why a bundled 7-Zip binary is required). That binary is bootstrapped on demand
from the network and gitignored, so this test is marked `local` and skips itself
when tools/7za.exe is not already present -- it never triggers a download.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from collections2mo2 import archive_inspect
from collections2mo2.sevenzip import TOOLS_DIR

_SEVENZIP_AVAILABLE = (TOOLS_DIR / "7za.exe").exists() and (TOOLS_DIR / "7z.dll").exists()


@pytest.mark.local
@pytest.mark.skipif(not _SEVENZIP_AVAILABLE, reason="tools/7za.exe not bootstrapped locally")
def test_inspect_archive_finds_fomod_and_layout(tmp_path: Path):
    archive_path = tmp_path / "test_mod.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", "<config><moduleName>Test</moduleName></config>")
        zf.writestr("meshes/x.nif", b"\x00\x01\x02\x03")

    result = archive_inspect.inspect_archive(archive_path)

    assert result["has_fomod"] is True
    assert result["fomod_dir"] == "fomod"
    assert result["layout"] == "data"
    assert result["file_count"] == 2
    assert result["archive_type"] == "zip"
