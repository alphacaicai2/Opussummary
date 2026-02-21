"""
Auto-bump patch version in VERSION file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def bump_patch(version: str) -> str:
    match = SEMVER_PATTERN.match(version.strip())
    if not match:
        raise ValueError(f"Invalid version format: {version!r}. Expected MAJOR.MINOR.PATCH")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def main() -> int:
    current = VERSION_FILE.read_text(encoding="utf-8").strip()
    next_version = bump_patch(current)
    VERSION_FILE.write_text(f"{next_version}\n", encoding="utf-8")
    print(next_version)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[bump_version] {exc}", file=sys.stderr)
        raise SystemExit(1)
