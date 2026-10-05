"""Settings: locations, the schema, loading, validation, and overrides.

Target design, *Configuration and paths* (DEC-8). Every setting is one row of
`SCHEMA`: its type, default, validation, `config show` order, and `init`'s
starter file all come from there, so adding a setting is one edit.

Operations here take arguments and return data or raise; they never print.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from downloadinux.errors import ConfigError

APP = "downloadinux"
LOCALTIME = Path("/etc/localtime")

STRING = "string"
PATH = "path"
INTEGER = "integer"
BOOLEAN = "boolean"

SOURCE_DEFAULT = "default"
SOURCE_FILE = "file"
SOURCE_OVERRIDE = "override"


# --- locations -------------------------------------------------------------

_XDG_DEFAULTS = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
}


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def xdg_dir(name: str, environ: Mapping[str, str] | None = None) -> Path:
    """An XDG base directory. Unset, empty, or relative values fall back to
    the `~` default, as the XDG specification requires (pinned list)."""
    value = _environ(environ).get(name, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / _XDG_DEFAULTS[name]


def user_path(value: str) -> Path:
    """Expand `~` and make a relative path absolute against the working
    directory, without resolving symlinks (used for `--config`,
    `DOWNLOADINUX_CONFIG`, and `--env`)."""
    return Path(os.path.abspath(os.path.expanduser(value)))


def settings_file(
    config: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """The settings file: `--config`, then `DOWNLOADINUX_CONFIG` (empty is
    unset), then `$XDG_CONFIG_HOME/downloadinux/config.toml` (DEC-8)."""
    if config:
        return user_path(config)
    from_env = _environ(environ).get("DOWNLOADINUX_CONFIG", "")
    if from_env:
        return user_path(from_env)
    return xdg_dir("XDG_CONFIG_HOME", environ) / APP / "config.toml"


def credentials_file(settings_path: Path) -> Path:
    """`credentials.toml` beside the settings file (DEC-8)."""
    return settings_path.with_name("credentials.toml")


# --- schema ----------------------------------------------------------------


@dataclass(frozen=True)
class Host:
    """What computed defaults read from the host, injectable for tests."""

    environ: Mapping[str, str] | None = None
    localtime: Path = LOCALTIME


def host_timezone(localtime: Path = LOCALTIME) -> str:
    """The part of `/etc/localtime`'s symlink target after `zoneinfo/`, else
    `UTC` (pinned list)."""
    try:
        target = os.readlink(localtime)
    except OSError:
        return "UTC"
    _, found, zone = target.partition("zoneinfo/")
    return zone if found and zone else "UTC"


@dataclass(frozen=True)
class Setting:
    key: str
    type: str
    default: Callable[[Host], Any]
    # A computed default is left commented out in the starter file, with
    # `comment` saying where it comes from.
    computed: bool = False
    comment: str = ""
    choices: tuple[str, ...] = ()

    @property
    def table(self) -> str:
        return self.key.partition(".")[0]

    @property
    def name(self) -> str:
        return self.key.partition(".")[2]


def _const(value: Any) -> Callable[[Host], Any]:
    return lambda host: value


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

SCHEMA: tuple[Setting, ...] = (
    Setting(
        "paths.downloads",
        PATH,
        _const(Path("~/Downloads")),
        comment="host output root; must exist",
    ),
    Setting(
        "paths.state",
        PATH,
        lambda host: xdg_dir("XDG_STATE_HOME", host.environ) / APP,
        computed=True,
        comment="default from XDG_STATE_HOME",
    ),
    Setting(
        "paths.cookies",
        PATH,
        lambda host: xdg_dir("XDG_DATA_HOME", host.environ) / APP / "cookies",
        computed=True,
        comment="default from XDG_DATA_HOME",
    ),
    Setting(
        "container.project",
        STRING,
        _const("downloadinux"),
        comment="Compose project name",
    ),
    Setting(
        "container.uid",
        INTEGER,
        lambda host: os.getuid(),
        computed=True,
        comment="the invoking host user's",
    ),
    Setting(
        "container.gid",
        INTEGER,
        lambda host: os.getgid(),
        computed=True,
        comment="the invoking host user's",
    ),
    Setting(
        "container.timezone",
        STRING,
        lambda host: host_timezone(host.localtime),
        computed=True,
        comment='the host\'s zone (from /etc/localtime), else "UTC"',
    ),
    Setting("downloads.user_agent", STRING, _const(DEFAULT_USER_AGENT)),
    Setting("vpn.enabled", BOOLEAN, _const(False)),
    Setting(
        "vpn.server",
        STRING,
        _const(""),
        comment="passed to `nordvpn connect`; empty = fastest",
    ),
    Setting(
        "vpn.technology",
        STRING,
        _const("openvpn"),
        comment='or "nordlynx"',
        choices=("openvpn", "nordlynx"),
    ),
    Setting("vpn.killswitch", BOOLEAN, _const(False)),
    Setting("vpn.autoconnect", BOOLEAN, _const(False)),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SCHEMA}
TABLES: tuple[str, ...] = tuple(dict.fromkeys(s.table for s in SCHEMA))


# --- validation ------------------------------------------------------------


def validate(setting: Setting, value: Any, where: str) -> Any:
    """Check a raw TOML value against its setting and return the typed value.

    `where` names the file (or `--set`) for the error message.
    """

    def fail(problem: str) -> ConfigError:
        return ConfigError(f"{where}: {setting.key}: {problem}")

    if setting.type in (STRING, PATH):
        if not isinstance(value, str):
            raise fail(f"expected a string, got {_toml_type(value)}")
        if setting.choices and value not in setting.choices:
            allowed = " or ".join(f'"{c}"' for c in setting.choices)
            raise fail(f'"{value}" is not allowed; expected {allowed}')
        if setting.type == PATH:
            # Expanded but not resolved, so a symlinked drive keeps the path
            # the user wrote (pinned list).
            path = Path(os.path.expanduser(value))
            if not path.is_absolute():
                raise fail(f'"{value}" is a relative path; use an absolute or ~ path')
            return path
        return value
    if setting.type == INTEGER:
        # bool is a subclass of int, and true is not a UID.
        if isinstance(value, bool) or not isinstance(value, int):
            raise fail(f"expected an integer, got {_toml_type(value)}")
        if value < 0:
            raise fail(f"expected a non-negative integer, got {value}")
        return value
    if setting.type == BOOLEAN:
        if not isinstance(value, bool):
            raise fail(f"expected true or false, got {_toml_type(value)}")
        return value
    raise AssertionError(f"unknown setting type {setting.type}")


def _toml_type(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "an array"
    return type(value).__name__


def read_toml(path: Path) -> dict[str, Any] | None:
    """A TOML file's contents, or None when it does not exist."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror}") from None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: not valid TOML: {error}") from None


def file_values(data: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Validate a parsed settings document into `{key: typed value}`."""
    values: dict[str, Any] = {}
    for table, body in data.items():
        if table not in TABLES:
            raise ConfigError(f"{where}: {table}: unknown table")
        if not isinstance(body, dict):
            raise ConfigError(
                f"{where}: {table}: expected a table, got {_toml_type(body)}"
            )
        for name, value in body.items():
            key = f"{table}.{name}"
            setting = BY_KEY.get(key)
            if setting is None:
                raise ConfigError(f"{where}: {key}: unknown setting")
            values[key] = validate(setting, value, where)
    return values


def parse_override(text: str) -> tuple[str, Any]:
    """One `--set table.key=value`, validated like a file value.

    A string setting takes the plain text after `=` unless the value is a
    quoted TOML string; other settings parse it as TOML, falling back to the
    plain text (pinned list). Paths are TOML strings, so they follow the
    string rule.
    """
    key, found, raw = text.partition("=")
    key = key.strip()
    if not found:
        raise ConfigError(f"--set: {text!r}: expected table.key=value")
    setting = BY_KEY.get(key)
    if setting is None:
        raise ConfigError(f"--set: {key}: unknown setting")
    parsed = _parse_toml_value(raw)
    if setting.type in (STRING, PATH):
        quoted = raw.strip()[:1] in ("'", '"') and isinstance(parsed, str)
        value: Any = parsed if quoted else raw
    else:
        value = raw if parsed is _UNPARSED else parsed
    return key, validate(setting, value, "--set")


_UNPARSED = object()


def _parse_toml_value(raw: str) -> Any:
    try:
        document = tomllib.loads(f"value = {raw}")
    except tomllib.TOMLDecodeError:
        return _UNPARSED
    if list(document) != ["value"]:
        return _UNPARSED
    return document["value"]


# --- loading ---------------------------------------------------------------


@dataclass(frozen=True)
class Effective:
    key: str
    value: Any
    source: str


@dataclass(frozen=True)
class Settings:
    """The effective settings, with the source of each, in schema order."""

    file: Path
    file_exists: bool
    entries: tuple[Effective, ...]

    def __getitem__(self, key: str) -> Any:
        for entry in self.entries:
            if entry.key == key:
                return entry.value
        raise KeyError(key)

    def source(self, key: str) -> str:
        for entry in self.entries:
            if entry.key == key:
                return entry.source
        raise KeyError(key)

    @property
    def credentials_file(self) -> Path:
        return credentials_file(self.file)


def defaults(host: Host = Host()) -> dict[str, Any]:
    """Every setting's built-in default, typed, in schema order."""
    values = {}
    for setting in SCHEMA:
        value = setting.default(host)
        if setting.type == PATH:
            value = Path(os.path.expanduser(value))
        values[setting.key] = value
    return values


def parse_overrides(overrides: Sequence[str]) -> dict[str, Any]:
    """`--set` values in order; a later one for the same key wins."""
    return dict(parse_override(text) for text in overrides)


def load(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> Settings:
    """Effective settings: `--set` > settings file > default (DEC-8).

    A missing settings file is not an error. Loading never checks that
    `paths.downloads` exists (pinned list).
    """
    path = settings_file(config, host.environ)
    from_set = parse_overrides(overrides)
    data = read_toml(path)
    from_file = {} if data is None else file_values(data, str(path))
    base = defaults(host)
    entries = []
    for setting in SCHEMA:
        key = setting.key
        if key in from_set:
            entries.append(Effective(key, from_set[key], SOURCE_OVERRIDE))
        elif key in from_file:
            entries.append(Effective(key, from_file[key], SOURCE_FILE))
        else:
            entries.append(Effective(key, base[key], SOURCE_DEFAULT))
    return Settings(file=path, file_exists=data is not None, entries=tuple(entries))
