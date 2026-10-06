"""The user-agent `config user-agent` pins: the host Chrome's major version.

Target design, *Host application* ("Downloads, pinned for P0001-C04"),
DEC-10, DEC-21. Stage 1 keeps today's source, the macOS Chrome bundle; Stage 2
replaces this lookup.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path
from xml.parsers.expat import ExpatError

# A module constant so tests can point it at a fixture.
CHROME_PLIST = Path("/Applications/Google Chrome.app/Contents/Info.plist")
VERSION_KEY = "CFBundleShortVersionString"

# Chrome freezes the macOS version at 10_15_7 and zeroes everything after the
# major in its user-agent.
TEMPLATE = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

_MAJOR = re.compile(r"[0-9]+")


def chrome_version(plist: Path) -> str | None:
    """`CFBundleShortVersionString` from `plist`, or None when the file is
    missing, unreadable, not a plist, or has no such string (DEC-21)."""
    try:
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, ExpatError):
        # plistlib.InvalidFileException is a ValueError; a truncated XML
        # plist raises ExpatError.
        return None
    version = data.get(VERSION_KEY) if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def major(version: str) -> str | None:
    """The text before the first `.`, when it is all (ASCII) digits."""
    head = version.partition(".")[0]
    return head if _MAJOR.fullmatch(head) else None


def user_agent(major: str) -> str:
    return TEMPLATE.format(major=major)
