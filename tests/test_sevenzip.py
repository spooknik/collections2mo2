"""Tests for sevenzip.ensure_7za's bootstrap serialisation, the hidden-console flag on child
processes, extract's tolerance of reparse-point errors, and the inspect stage's handling of
archives that fail to list.

None of these touch the network or need tools/7za.exe: the bootstrap and the listing are
replaced with fakes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from collections2mo2 import archive_inspect, sevenzip
from collections2mo2.reporter import NullReporter


def test_ensure_7za_bootstraps_once_under_concurrency(tmp_path: Path, monkeypatch):
    """Eight workers arriving at once must produce one bootstrap, not eight racing ones."""
    monkeypatch.setattr(sevenzip, "TOOLS_DIR", tmp_path / "tools")
    calls: list[int] = []
    lock = threading.Lock()

    def fake_bootstrap(exe: Path, dll: Path) -> None:
        with lock:
            calls.append(1)
        time.sleep(0.05)  # long enough for every other worker to queue up behind the lock
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ")
        dll.write_bytes(b"MZ")

    monkeypatch.setattr(sevenzip, "_bootstrap_7za", fake_bootstrap)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: sevenzip.ensure_7za(), range(8)))

    assert len(calls) == 1
    assert all(r == tmp_path / "tools" / "7za.exe" for r in results)
    # A later call finds the binary and never bootstraps again.
    assert sevenzip.ensure_7za() == tmp_path / "tools" / "7za.exe"
    assert len(calls) == 1


def test_run_7z_hides_console_window(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sevenzip.subprocess, "run", fake_run)
    sevenzip._run_7z(Path("7za.exe"), ["l"])

    assert seen["capture_output"] is True
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        assert seen["creationflags"] == 0


class _RecordingReporter(NullReporter):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def test_cmd_inspect_records_failures(tmp_path: Path, monkeypatch):
    """An archive that cannot be listed ends up in inspect.json's `failures` with its error,
    is warned about through the reporter (the GUI log), and makes the stage exit 1."""
    good = tmp_path / "good.zip"
    bad = tmp_path / "bad.zip"
    good.write_bytes(b"")
    bad.write_bytes(b"")
    downloads = tmp_path / "downloads.json"
    downloads.write_text(
        json.dumps(
            {
                "entries": [
                    {"name": "Good Mod", "file_name": good.name, "path": str(good), "status": "ok"},
                    {"name": "Bad Mod", "file_name": bad.name, "path": str(bad), "status": "ok"},
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(archive_inspect, "ensure_7za", lambda: Path("7za.exe"))

    def fake_list(path):
        if Path(path) == bad:
            raise RuntimeError("7za l failed (exit 2): Cannot open the file as archive")
        return [sevenzip.ArchiveEntry(path="meshes/x.nif", size=4, is_dir=False)]

    monkeypatch.setattr(archive_inspect, "list_archive", fake_list)

    rep = _RecordingReporter()
    args = argparse.Namespace(downloads_json=str(downloads), out=None, jobs=2)
    rc = archive_inspect.cmd_inspect(args, reporter=rep)

    assert rc == 1
    written = json.loads((tmp_path / "inspect.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in written["entries"]] == ["Good Mod"]
    assert written["failures"] == [
        {
            "name": "Bad Mod",
            "file_name": "bad.zip",
            "error": "7za l failed (exit 2): Cannot open the file as archive",
        }
    ]
    assert any("Bad Mod" in w and "Cannot open the file" in w for w in rep.warnings)


def test_cmd_inspect_reports_bootstrap_failure_once(tmp_path: Path, monkeypatch):
    """If 7-Zip cannot be set up, that is one stage error, not one failure per archive."""
    downloads = tmp_path / "downloads.json"
    downloads.write_text(
        json.dumps(
            {
                "entries": [
                    {"name": f"Mod {i}", "path": str(tmp_path / f"{i}.zip"), "status": "ok"}
                    for i in range(3)
                ]
            }
        ),
        encoding="utf-8",
    )

    def boom():
        raise RuntimeError("download of 7zr.exe failed")

    monkeypatch.setattr(archive_inspect, "ensure_7za", boom)
    monkeypatch.setattr(
        archive_inspect, "list_archive", lambda p: pytest.fail("must not list without 7-Zip")
    )

    rep = _RecordingReporter()
    rc = archive_inspect.cmd_inspect(
        argparse.Namespace(downloads_json=str(downloads), out=None, jobs=2), reporter=rep
    )

    assert rc == 1
    assert rep.warnings == ["inspect: could not set up 7-Zip: download of 7zr.exe failed"]
    assert not (tmp_path / "inspect.json").exists()


def _fake_extract_env(monkeypatch, tmp_path: Path, *, returncode: int, output: str, entries):
    """Replace 7za with a fake that writes `entries` ({path: size}) into dest and exits as told."""
    monkeypatch.setattr(sevenzip, "ensure_7za", lambda: Path("7za.exe"))

    def fake_run(exe, args):
        dest = Path(next(a for a in args if a.startswith("-o"))[2:])
        for rel, size in entries.items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x" * size)
        return subprocess.CompletedProcess([str(exe), *args], returncode, stdout=output, stderr="")

    monkeypatch.setattr(sevenzip, "_run_7z", fake_run)
    monkeypatch.setattr(
        sevenzip,
        "list_archive",
        lambda path: [
            sevenzip.ArchiveEntry(path="Mod", size=0, is_dir=True),
            *(sevenzip.ArchiveEntry(path=p, size=n, is_dir=False) for p, n in entries.items()),
        ],
    )


REPARSE_OUTPUT = (
    "ERROR: Incorrect reparse stream : Unspecified error : Mod\\HowToMakeItWork.txt\n"
    "\nSub items Errors: 1\n"
)


def test_extract_tolerates_reparse_point_errors_when_files_are_complete(tmp_path, monkeypatch):
    """A zip made from a OneDrive folder carries reparse attributes on plain files; 7-Zip writes
    the bytes, fails to reapply the attribute and exits 2. That is a warning, not a failure."""
    files = {"Mod/HowToMakeItWork.txt": 161, "Mod/textures/readme.txt": 592}
    _fake_extract_env(monkeypatch, tmp_path, returncode=2, output=REPARSE_OUTPUT, entries=files)

    warnings = sevenzip.extract("mod.zip", tmp_path / "out")

    assert warnings == [
        (
            "7za x reported (exit 2): ERROR: Incorrect reparse stream : Unspecified error : "
            "Mod\\HowToMakeItWork.txt"
        )
    ]
    assert (tmp_path / "out" / "Mod" / "HowToMakeItWork.txt").stat().st_size == 161


def test_extract_clean_run_returns_no_warnings(tmp_path, monkeypatch):
    _fake_extract_env(monkeypatch, tmp_path, returncode=0, output="", entries={"Mod/a.esp": 3})
    assert sevenzip.extract("mod.zip", tmp_path / "out") == []


def test_extract_still_fails_when_a_file_is_missing_or_short(tmp_path, monkeypatch):
    files = {"Mod/HowToMakeItWork.txt": 161}
    _fake_extract_env(monkeypatch, tmp_path, returncode=2, output=REPARSE_OUTPUT, entries=files)
    # The listing promises a second file the fake never wrote.
    monkeypatch.setattr(
        sevenzip,
        "list_archive",
        lambda path: [
            sevenzip.ArchiveEntry(path="Mod/HowToMakeItWork.txt", size=161, is_dir=False),
            sevenzip.ArchiveEntry(path="Mod/big.bsa", size=10, is_dir=False),
        ],
    )
    with pytest.raises(RuntimeError, match="7za x failed"):
        sevenzip.extract("mod.zip", tmp_path / "out")


def test_extract_still_fails_on_data_errors(tmp_path, monkeypatch):
    """A CRC failure also leaves a full-size file behind; only link/reparse errors are benign."""
    files = {"Mod/a.esp": 5}
    output = "ERROR: CRC Failed : Mod\\a.esp\n\nSub items Errors: 1\n"
    _fake_extract_env(monkeypatch, tmp_path, returncode=2, output=output, entries=files)
    with pytest.raises(RuntimeError, match="CRC Failed"):
        sevenzip.extract("mod.zip", tmp_path / "out")
