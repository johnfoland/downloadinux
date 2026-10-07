"""`config import-env`: turn an existing `.env` into the settings and
credentials files and copy the cookie jar (DEC-13).

Target design, *Migration*, and its pinned list. The `.env` and the old jar
are only read. Nothing here prints; the result says what happened, and never
carries the token's value.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux import credentials, files, settings
from downloadinux.checkout import checkout_root
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.settings import BY_KEY, Host

COOKIE_JAR = "cookies.txt"


def parse_env(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines: blank and `#` lines skipped, an `export ` prefix
    allowed, one pair of matching surrounding quotes stripped, no
    interpolation. A line without `=` is a key with an empty value, and a
    later duplicate wins (DEC-14)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _integer(value: str) -> Any:
    # Left as text when it is not a plain integer, so validation reports it.
    return int(value) if value.isascii() and value.isdigit() else value


def _choice(mapping: dict[str, bool]) -> Callable[[str], Any]:
    return lambda value: mapping.get(value, value)


_TRUE_FALSE = {"true": True, "false": False}
_ON_OFF = {"on": True, "true": True, "off": False, "false": False}

# `.env` key → (setting, conversion). The conversion returns the setting's
# type, or the text unchanged for validation to reject.
MAPPING: dict[str, tuple[str, Callable[[str], Any]]] = {
    "HOST_DOWNLOADS": ("paths.downloads", str),
    "PUID": ("container.uid", _integer),
    "PGID": ("container.gid", _integer),
    "TZ": ("container.timezone", str),
    "NORDVPN_SERVER": ("vpn.server", str),
    "NORDVPN_TECHNOLOGY": ("vpn.technology", str),
    "NORDVPN_AUTOCONNECT": ("vpn.autoconnect", _choice(_TRUE_FALSE)),
    "NORDVPN_KILLSWITCH": ("vpn.killswitch", _choice(_ON_OFF)),
}
TOKEN_KEY = "NORDVPN_TOKEN"

_ALLOWED = {
    "NORDVPN_AUTOCONNECT": "true or false",
    "NORDVPN_KILLSWITCH": "on, off, true, or false",
}


class JarOutcome(str, Enum):
    COPIED = "copied"
    KEPT = "kept"  # the destination already had a jar
    NO_SOURCE = "no source"


@dataclass(frozen=True)
class ImportResult:
    env_file: Path
    settings_file: Path
    # (setting key, typed value) imported from `.env`, in schema order.
    imported: tuple[tuple[str, Any], ...]
    credentials_file: Path | None  # None when no token was imported
    token: credentials.TokenState
    not_imported: tuple[str, ...]
    jar: JarOutcome
    jar_source: Path
    jar_destination: Path
    warnings: tuple[str, ...] = field(default=())


def default_env_file() -> Path:
    return checkout_root() / ".env"


def convert(env: dict[str, str], where: str) -> tuple[dict[str, Any], list[str]]:
    """Mapped settings, typed and validated, and the keys not imported."""
    imported: dict[str, Any] = {}
    not_imported: list[str] = []
    for key, raw in env.items():
        if key == TOKEN_KEY:
            continue
        if key not in MAPPING:
            not_imported.append(key)
            continue
        if raw == "":
            continue  # keeps the setting's default
        setting_key, conversion = MAPPING[key]
        value = conversion(raw)
        if key in _ALLOWED and not isinstance(value, bool):
            raise ConfigError(f'{where}: {key}: "{raw}" is not {_ALLOWED[key]}')
        imported[setting_key] = settings.validate(BY_KEY[setting_key], value, where, key)
    return imported, not_imported


def import_env(
    env_file: str | None = None,
    force: bool = False,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> ImportResult:
    env_path = settings.user_path(env_file) if env_file else default_env_file()
    try:
        env = parse_env(env_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DownloadinuxError(f"{env_path}: no such file") from None
    except OSError as error:
        raise DownloadinuxError(f"{env_path}: {error.strerror}") from None
    except UnicodeDecodeError as error:
        raise DownloadinuxError(f"{env_path}: cannot read: {error}") from None

    imported, not_imported = convert(env, str(env_path))
    from_set = settings.parse_overrides(overrides)

    warnings: list[str] = []
    token = env.get(TOKEN_KEY, "")
    if token and not credentials.token_is_valid(token):
        warnings.append(
            f"{env_path}: {TOKEN_KEY} is malformed ({len(token)} characters); "
            "not imported"
        )
        token = ""

    settings_path = settings.settings_file(config, host.environ)
    creds_path = settings.credentials_file(settings_path)
    # Every refusal is checked before anything is written.
    if not force:
        if settings_path.exists():
            raise DownloadinuxError(
                f"{settings_path} already exists; use --force to overwrite it"
            )
        if token and creds_path.exists():
            raise DownloadinuxError(
                f"{creds_path} already exists; use --force to overwrite it"
            )

    values = {**imported, "vpn.enabled": True, **from_set}
    try:
        files.write_atomic(
            settings_path,
            files.starter_text(values, host),
            files.default_file_mode(),
            replace=force,
        )
    except FileExistsError:
        raise DownloadinuxError(
            f"{settings_path} already exists; use --force to overwrite it"
        ) from None
    if token:
        try:
            files.write_atomic(
                creds_path, files.credentials_text(token), files.CREDENTIALS_MODE,
                replace=force,
            )
        except FileExistsError:
            raise DownloadinuxError(
                f"{creds_path} already exists; use --force to overwrite it"
            ) from None

    cookies_dir = settings.load(config, overrides, host)["paths.cookies"]
    source = env_path.parent / "cookies" / COOKIE_JAR
    destination = cookies_dir / COOKIE_JAR
    jar = copy_jar(source, destination)

    ordered = tuple(
        (s.key, values[s.key])
        for s in settings.SCHEMA
        if s.key in imported or s.key == "vpn.enabled"
    )
    return ImportResult(
        env_file=env_path,
        settings_file=settings_path,
        imported=ordered,
        credentials_file=creds_path if token else None,
        token=credentials.TokenState.SET if token else credentials.TokenState.UNSET,
        not_imported=tuple(not_imported),
        jar=jar,
        jar_source=source,
        jar_destination=destination,
        warnings=tuple(warnings),
    )


def copy_jar(source: Path, destination: Path) -> JarOutcome:
    """Copy the jar into the managed cookies directory (created 0700, jar
    0600). An existing destination jar is kept, even with `--force`."""
    if not source.is_file():
        return JarOutcome.NO_SOURCE
    if destination.exists():
        return JarOutcome.KEPT
    directory = destination.parent
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)  # mkdir's mode is subject to the umask
    data = source.read_bytes()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
    return JarOutcome.COPIED
