"""Bootstrap and wrap a standalone 7-Zip console binary (tools/7za.exe).

There is no system 7-Zip on this machine, and Nexus Mods archives are frequently
.rar, which py7zr cannot read. We bootstrap a real 7-Zip console binary into
tools/ (never installed system-wide) and drive it as a subprocess from then on
for listing/extracting real mod archives.

This takes three downloads from the official 7-Zip GitHub release (the same
release https://www.7-zip.org/download.html points at), because none of them
alone gets us a RAR-capable, portable console binary:

1. SEVENZIP_REDUCED_URL (7zr.exe) -- a tiny "reduced" 7-Zip console tool shipped
   as a plain, uncompressed .exe. py7zr (already a project dependency) turns out
   to be unusable for any of this bootstrapping: every PE binary 7-Zip ships
   (7za.exe, 7z.exe, ...) is compressed in its release archives with the BCJ2
   filter, which py7zr does not implement (py7zr.compressor raises
   UnsupportedCompressionMethodError for P7Z_BCJ2 -- this is a known, long-standing
   py7zr limitation, not specific to one release). 7zr.exe, being uncompressed,
   needs no extraction at all and gives us a real 7-Zip binary that *can* decode
   BCJ2.
2. SEVENZIP_EXTRA_URL (7z2602-extra.7z) -- the "extra" console package containing
   7za.exe/7za.dll/7zxa.dll, BCJ2-compressed. We use 7zr.exe from step 1 to
   extract it. This 7za.exe is real 7-Zip, but the "reduced" codec set it's
   built with has no RAR decoder (confirmed via `7za i`) -- it only lists
   7z/Cab/bzip2/gzip/tar/xz/zip/zstd.
3. SEVENZIP_MSI_URL (7z2602-x64.msi) -- the official Windows Installer package.
   An .msi is itself readable as an archive (an embedded Cab/LZX stream), which
   the step-2 7za.exe can open. Inside it are `_7z.exe` and `_7z.dll`: the full
   console tool + its main codec DLL, which *does* include a RAR/RAR5 decoder.
   We extract just those two files -- no msiexec, no registry writes, nothing
   installed outside tools/ -- rename `_7z.exe` to 7za.exe (overwriting the
   reduced build from step 2) and `_7z.dll` to 7z.dll (7z.exe hard-codes that
   filename, looked up next to itself, regardless of the .exe's own name).

End state: tools/7za.exe + tools/7z.dll, which together handle .7z/.zip/.rar.

`ensure_7za` is called from every listing/extraction, including the parallel workers of
the inspect and install stages, so the bootstrap is serialised with a module-level lock:
on a first run four workers would otherwise all download and unpack 7-Zip into the same
folder at once, deleting each other's staging files mid-extraction, and every archive in
flight during those seconds would fail to list (seen on the first frozen-app run of a
1,966-mod collection: the first 18 archives failed, everything after them was fine).

Child processes are started with CREATE_NO_WINDOW. The frozen GUI has no console of its
own (PyInstaller `console=False`), so without it every 7za.exe call pops up a console
window for a fraction of a second and steals focus.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlretrieve

# Repo root is two levels above this file: src/collections2mo2/sevenzip.py
REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"

# 7-Zip 26.02 release assets, as linked from https://www.7-zip.org/download.html
# (that page points at github.com/ip7z/7zip releases).
_SEVENZIP_RELEASE = "https://github.com/ip7z/7zip/releases/download/26.02"
SEVENZIP_REDUCED_URL = f"{_SEVENZIP_RELEASE}/7zr.exe"
SEVENZIP_EXTRA_URL = f"{_SEVENZIP_RELEASE}/7z2602-extra.7z"
SEVENZIP_MSI_URL = f"{_SEVENZIP_RELEASE}/7z2602-x64.msi"

# Members inside the extra package (step 2), mapped to their staging name in
# tools/. We take the x64 build (this machine is x64); 7zxa.dll only ships as
# one arch-independent build at the archive root and ends up unused (RAR support
# comes from step 3) but is extracted for completeness/inspection.
_EXTRA_MEMBERS = {
    "x64/7za.exe": "7za.exe",
    "x64/7za.dll": "7za.dll",
    "7zxa.dll": "7zxa.dll",
}
# Members inside the MSI (step 3): full console tool + its codec DLL (has RAR).
_MSI_MEMBERS = {
    "_7z.exe": "7za.exe",
    "_7z.dll": "7z.dll",
}

# Windows-only flag (0 elsewhere): run console children without creating a console window.
# Shared by every subprocess call site in the package (robocopy, wabbajack-cli) so a windowed
# frozen build never flashes terminals; output is captured through pipes regardless.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_BOOTSTRAP_LOCK = threading.Lock()


@dataclass
class ArchiveEntry:
    path: str
    size: int
    is_dir: bool


def _run_7z(exe: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=NO_WINDOW,
    )


def ensure_7za() -> Path:
    """Return the path to tools/7za.exe, bootstrapping it if needed (see module docstring).

    Safe to call from many threads at once: the first caller bootstraps while the rest wait
    on the lock and then find the finished binary.
    """
    exe = TOOLS_DIR / "7za.exe"
    dll = TOOLS_DIR / "7z.dll"
    if exe.exists() and dll.exists():
        return exe
    with _BOOTSTRAP_LOCK:
        # Re-resolve under the lock: TOOLS_DIR is reassigned by api.py for the frozen app,
        # and another thread may have finished the bootstrap while we waited.
        exe = TOOLS_DIR / "7za.exe"
        dll = TOOLS_DIR / "7z.dll"
        if exe.exists() and dll.exists():
            return exe
        _bootstrap_7za(exe, dll)
    return exe


def _bootstrap_7za(exe: Path, dll: Path) -> None:
    """Download and assemble tools/7za.exe + tools/7z.dll (the three steps in the docstring)."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    bootstrap = TOOLS_DIR / "_7zr-bootstrap.exe"
    extra_bundle = TOOLS_DIR / "_7z-extra-download.7z"
    msi_bundle = TOOLS_DIR / "_7z-msi-download.msi"
    try:
        urlretrieve(SEVENZIP_REDUCED_URL, bootstrap)
        urlretrieve(SEVENZIP_EXTRA_URL, extra_bundle)

        result = _run_7z(
            bootstrap, ["x", "-y", f"-o{TOOLS_DIR}", str(extra_bundle), *_EXTRA_MEMBERS]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"bootstrap extraction of {SEVENZIP_EXTRA_URL} failed "
                f"(exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        for src, dst in _EXTRA_MEMBERS.items():
            (TOOLS_DIR / src).replace(TOOLS_DIR / dst)
        x64_dir = TOOLS_DIR / "x64"
        if x64_dir.is_dir() and not any(x64_dir.iterdir()):
            x64_dir.rmdir()

        # Step 3: pull the RAR-capable full console tool + codec DLL out of the MSI,
        # using the reduced 7za.exe we just built (it can read Cab/LZX inside the MSI).
        urlretrieve(SEVENZIP_MSI_URL, msi_bundle)
        result = _run_7z(exe, ["x", "-y", f"-o{TOOLS_DIR}", str(msi_bundle), *_MSI_MEMBERS])
        if result.returncode != 0:
            raise RuntimeError(
                f"extraction of {SEVENZIP_MSI_URL} failed "
                f"(exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        for src, dst in _MSI_MEMBERS.items():
            (TOOLS_DIR / src).replace(TOOLS_DIR / dst)

        # The reduced-build 7za.dll/7zxa.dll from step 2 are unused now that
        # 7za.exe is the full build (which needs 7z.dll, not 7za.dll).
        (TOOLS_DIR / "7za.dll").unlink(missing_ok=True)
        (TOOLS_DIR / "7zxa.dll").unlink(missing_ok=True)
    finally:
        bootstrap.unlink(missing_ok=True)
        extra_bundle.unlink(missing_ok=True)
        msi_bundle.unlink(missing_ok=True)

    if not exe.exists() or not dll.exists():
        raise RuntimeError(f"7za.exe/7z.dll not found after bootstrapping from {_SEVENZIP_RELEASE}")

    result = _run_7z(exe, [])
    if "7-Zip" not in result.stdout:
        raise RuntimeError(f"7za.exe did not report itself as 7-Zip: {result.stdout!r}")


def _entry_from_block(block: dict[str, str]) -> ArchiveEntry:
    raw_path = block.get("Path", "")
    size = int(block.get("Size") or 0)
    attrs = block.get("Attributes", "")
    folder = block.get("Folder", "")
    is_dir = folder == "+" or "D" in attrs
    return ArchiveEntry(path=raw_path.replace("\\", "/"), size=size, is_dir=is_dir)


def list_archive(path: Path | str) -> list[ArchiveEntry]:
    """List entries in a .7z/.zip/.rar archive via `7za l -slt -ba`."""
    exe = ensure_7za()
    result = _run_7z(exe, ["l", "-slt", "-ba", str(path)])
    if result.returncode != 0:
        raise RuntimeError(
            f"7za l failed for {path} (exit {result.returncode}): {result.stderr.strip()}"
        )

    entries: list[ArchiveEntry] = []
    current: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            if current:
                entries.append(_entry_from_block(current))
                current = {}
            continue
        key, sep, value = line.partition(" = ")
        if not sep:
            continue
        current[key.strip()] = value.strip()
    if current:
        entries.append(_entry_from_block(current))
    return entries


def extract(path: Path | str, dest: Path | str, members: list[str] | None = None) -> None:
    """Extract an archive (or specific members of it) into `dest` via `7za x`."""
    exe = ensure_7za()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    args = ["x", "-y", f"-o{dest}", str(path)]
    if members:
        args.extend(members)
    result = _run_7z(exe, args)
    if result.returncode != 0:
        raise RuntimeError(
            f"7za x failed for {path} (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
