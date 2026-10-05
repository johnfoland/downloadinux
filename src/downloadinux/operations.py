"""Operations behind the CLI's subcommands, callable without argparse.

Each takes arguments and returns data or raises a `DownloadinuxError`; the
CLI (and later the TUI) only parses and presents.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from downloadinux import credentials, settings
from downloadinux.mounts import mounts
from downloadinux.settings import Host


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


def show_config(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> dict[str, Any]:
    """`config show`: effective settings with sources, the files, the token's
    state (never its value), and the mounts, shaped as `--json` prints them
    (target design, *Configuration and paths*, pinned list)."""
    effective = settings.load(config, overrides, host)
    creds = credentials.load(effective.credentials_file)
    return {
        "config_file": {
            "path": str(effective.file),
            "exists": effective.file_exists,
        },
        "credentials_file": {"path": str(creds.file), "exists": creds.file_exists},
        "settings": [
            {"key": e.key, "value": _plain(e.value), "source": e.source}
            for e in effective.entries
        ],
        "token": creds.token_state.value,
        "mounts": [
            {
                "container": m.container,
                "host": str(m.host),
                "access": m.access,
                "exists": m.exists,
            }
            for m in mounts(effective)
        ],
    }
