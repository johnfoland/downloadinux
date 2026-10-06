"""Operations behind the CLI's subcommands, callable without argparse.

Each takes arguments and returns data or raises a `DownloadinuxError`; the
CLI (and later the TUI) only parses and presents.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from downloadinux import compose, credentials, files, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.mounts import mounts
from downloadinux.settings import Host


def run_compose(
    args: Sequence[str],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """Pass arguments through unchanged, preserving Docker's exit status."""
    effective = settings.load(config, overrides, host)
    creds = credentials.load(effective.credentials_file)
    return compose.invocation(effective, creds, host.environ).run(args, runner)


LIFECYCLE = {
    "build": (("build",),),
    "up": (("up", "-d"),),
    "down": (("down",),),
    "restart": (("up", "-d", "--force-recreate"),),
    "update": (("build", "--no-cache"), ("up", "-d")),
}


def lifecycle(
    command: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> None:
    """Container lifecycle, per target design, Host application's C02 list."""
    effective = settings.load(config, overrides, host)
    if command in {"up", "restart", "update"}:
        downloads = effective["paths.downloads"]
        if not downloads.is_dir():
            raise DownloadinuxError(f"{downloads}: downloads directory missing; create or mount it before starting")
        effective["paths.state"].mkdir(parents=True, exist_ok=True)
        cookies = effective["paths.cookies"]
        if not cookies.exists():
            cookies.mkdir(parents=True, mode=0o700, exist_ok=True)
            cookies.chmod(0o700)
    creds = credentials.load(effective.credentials_file)
    invocation = compose.invocation(effective, creds, host.environ)
    for args in LIFECYCLE[command]:
        status = invocation.run(args, runner)
        if status != 0:
            raise DownloadinuxError(f"docker: Compose {args[0]} failed (exit {status})")


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


def init(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> Path:
    """`init`: write the starter settings file (VPN off, DEC-11) with any
    `--set` values in it. Refuses if the file exists; there is no `--force`
    (target design, *Configuration and paths*, pinned list)."""
    values = settings.parse_overrides(overrides)
    path = settings.settings_file(config, host.environ)
    if path.exists():
        raise DownloadinuxError(f"{path} already exists; not overwriting it")
    try:
        files.write_atomic(
            path,
            files.starter_text(values, host),
            files.default_file_mode(),
            replace=False,
        )
    except FileExistsError:
        raise DownloadinuxError(f"{path} already exists; not overwriting it") from None
    return path


def malformed_token_message(token: str) -> str:
    """Names the token's length, never its value."""
    return (
        f"the token is malformed ({len(token)} characters); "
        "expected 64 lowercase hex characters"
    )


def store_token(path: Path, token: str) -> None:
    """Write `token` into the credentials file at `path` (mode 0600), keeping
    an existing file's other content and comments."""
    existing = credentials.load(path)  # rejects an invalid existing file
    text = path.read_text(encoding="utf-8") if existing.file_exists else None
    files.write_atomic(
        path, files.credentials_text(token, text), files.CREDENTIALS_MODE
    )


def set_token(
    token: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> Path:
    """`config set-token`: validate and store the NordVPN token. It only
    stores; logging in to a running container comes with the lifecycle
    commands (P0001 D2)."""
    effective = settings.load(config, overrides, host)
    if not credentials.token_is_valid(token):
        raise DownloadinuxError(malformed_token_message(token))
    store_token(effective.credentials_file, token)
    return effective.credentials_file
