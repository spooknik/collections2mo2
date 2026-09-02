"""Pre-commit hook: refuse to commit anything that looks like a Nexus API key or a .env file.

Usage: python scripts/check_secrets.py <files...>
Exit 1 if any file is a .env variant (other than .env.example) or contains a key-shaped value.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

KEY_PATTERNS = [
    re.compile(r"NEXUS_API_KEY\s*=\s*['\"]?[A-Za-z0-9+/=_-]{20,}"),
    re.compile(r"apikey['\"]?\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{20,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
ALLOWED_ENV = {".env.example"}


def main(argv: list[str]) -> int:
    bad: list[str] = []
    for arg in argv:
        path = Path(arg)
        name = path.name
        if name.startswith(".env") and name not in ALLOWED_ENV:
            bad.append(f"{arg}: .env files must never be committed")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in KEY_PATTERNS:
            if pattern.search(text):
                bad.append(f"{arg}: contains a value matching {pattern.pattern!r}")
                break
    for line in bad:
        print(f"secret check: {line}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
