"""
Single source of truth for the application version.
"""

from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
_DEFAULT_VERSION = "0.1.0"


def get_version() -> str:
    """Return app version from VERSION file, with a safe fallback."""
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return value or _DEFAULT_VERSION
    except Exception:
        return _DEFAULT_VERSION
