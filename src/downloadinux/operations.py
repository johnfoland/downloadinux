"""Operations behind the CLI's subcommands, callable without argparse.

Each takes arguments and returns data or raises a `DownloadinuxError`; the
CLI (and later the TUI) only parses and presents.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux import compose, cookiejar, cookie_selectors, cookie_store, cookie_writer, credentials, files, health, servers, settings, useragent
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.mounts import mounts
from downloadinux.settings import Host


def _cookie_name(name: str) -> None:
    if type(name) is not str or not cookie_store.NAME.fullmatch(name):
        raise ConfigError('source name must match [a-z0-9][a-z0-9_-]{0,63}')


def cookie_browsers(*, platform: str | None = None) -> dict:
    """Capabilities only; no installation probe, settings, or migration (§2)."""
    return cookie_selectors.support(platform=platform)


def _browser_export(selector: cookie_selectors.Selector, exporter=None) -> cookiejar.Material:
    if exporter is None:
        from downloadinux.cookie_browser import extract
        exporter = extract
    return exporter(selector)


def _cookie_origin_material(origin: dict, exporter=None) -> cookiejar.Material:
    """Registration and explicit refresh share the same origin boundary (§2)."""
    if origin['kind'] == 'file':
        try:
            return cookiejar.read(Path(origin['path']))
        except DownloadinuxError as error:
            raise DownloadinuxError(f'{error}; check the origin file or replace this source with cookies import NAME FILE --replace') from None
    try:
        selector = cookie_selectors.normalize(origin['browser'], origin['profile'],
                                               origin['keyring'], origin['container'])
    except ConfigError as error:
        # Keep §2's exit-2 selector contract even when refreshing a stored
        # origin on a different platform; extraction failures still exit 1.
        raise ConfigError(f'{error}; replace the origin with cookies add-browser NAME --browser BROWSER --replace, or use Netscape import') from None
    return _browser_export(selector, exporter)


def _publish_cookie_report(root: Path, name: str, material: cookiejar.Material,
                           origin: dict, expected: cookie_store.Source | None,
                           now: float | None, *, select: bool = False) -> dict:
    # Return committed metadata without fallible I/O after publication or
    # letting a subsequent refresh substitute its revision in our summary.
    snapshot = cookie_writer.publish_snapshot(root, name, material, origin,
                                              expected=expected, select=select)
    return dict(source=cookie_store.source_metadata(snapshot, name),
                **cookiejar.summary(material, now=now))


def _register_cookie_source(name: str, origin: dict, read_material, *, replace: bool,
                            config: str | None, overrides: Sequence[str], host: Host,
                            now: float | None, select: bool = False) -> dict:
    """Capture before extraction; compare under the real writer lock (§1–§2)."""
    root = settings.load(config, overrides, host)['paths.cookies']
    try:
        snapshot = cookie_writer.ensure_store(root)
        expected = snapshot.sources.get(name)
        if expected is not None and not replace:
            raise DownloadinuxError(f'{root / "sources.json"}: already registered; use --replace to replace its origin/material')
        material = read_material()
        return _publish_cookie_report(root, name, material, origin, expected, now, select=select)
    except ConfigError:
        raise
    except DownloadinuxError as error:
        raise DownloadinuxError(f'source {name}: {error}') from None


def import_cookies(name: str, file: str, *, replace: bool = False,
                   config: str | None = None, overrides: Sequence[str] = (),
                   host: Host = Host(), now: float | None = None) -> dict:
    """Register a managed Netscape copy; external origins stay untouched (§2)."""
    _cookie_name(name)
    if type(file) is not str or not file or '\x00' in file:
        raise ConfigError('cookie file must be a nonempty path without NUL')
    try:
        path = settings.user_path(file)
    except OSError as error:
        raise DownloadinuxError(f'source {name}: {file}: {error.strerror or "cannot normalize file path"}; check the file path') from None
    origin = dict(kind='file', path=str(path))
    return _register_cookie_source(name, origin,
                                   lambda: _cookie_origin_material(origin), replace=replace,
                                   config=config, overrides=overrides, host=host, now=now)


def add_browser_cookies(name: str, browser: str, *, profile: str | None = None,
                        keyring: str | None = None, container: str | None = None,
                        replace: bool = False, config: str | None = None,
                        overrides: Sequence[str] = (), host: Host = Host(),
                        exporter=None, now: float | None = None) -> dict:
    """Export before publishing; registration never changes the default (§2)."""
    _cookie_name(name)
    selector = cookie_selectors.normalize(browser, profile, keyring, container)
    origin = selector.origin()
    return _register_cookie_source(name, origin,
                                   lambda: _cookie_origin_material(origin, exporter), replace=replace,
                                   config=config, overrides=overrides, host=host, now=now)


FIREFOX_SOURCE = 'firefox'


def firefox_cookies(profile: str | None = None, config: str | None = None,
                    overrides: Sequence[str] = (), host: Host = Host(),
                    *, exporter=None, now: float | None = None) -> dict:
    """The account switch `just cookies` calls from the cutover: create or
    replace the source firefox, whatever its kind, and select it in the same
    publication (§5, DEC-38).

    No keyring or container, so the export covers all Firefox containers.
    Errors and the summary are add-browser's; no site or user-agent lookup.
    """
    selector = cookie_selectors.normalize('firefox', profile, None, None)
    origin = selector.origin()
    return _register_cookie_source(FIREFOX_SOURCE, origin,
                                   lambda: _cookie_origin_material(origin, exporter),
                                   replace=True, select=True, config=config,
                                   overrides=overrides, host=host, now=now)


SETUP_PROMPT = 'Export Firefox cookies now? [y/N] '


@dataclass(frozen=True)
class CookieSetup:
    """`cookies setup`'s outcome: `kept` names a named default left alone;
    `report` is its summary, or the new firefox source's after an export,
    and is None when the default stays anonymous."""

    kept: str | None
    report: dict | None


def cookie_setup(config: str | None = None, overrides: Sequence[str] = (),
                 host: Host = Host(), *, ask, exporter=None,
                 now: float | None = None) -> CookieSetup:
    """Setup's cookie step (§5, DEC-38). `ask(prompt)` returns one line and
    raises `EOFError` at end of input, which is a no.

    Migrates first. A named default is inspected and kept, never refreshed;
    an anonymous default asks once, and only y/yes exports through
    `firefox_cookies` with default discovery. Settings and the user-agent
    are never touched.
    """
    root = settings.load(config, overrides, host)['paths.cookies']
    snapshot = cookie_writer.ensure_store(root)
    if snapshot.default is not None:
        report = cookie_store.inspect(root, snapshot, snapshot.default, now=now)
        return CookieSetup(snapshot.default, report)
    try:
        answer = ask(SETUP_PROMPT)
    except EOFError:
        answer = ''
    if answer.strip().lower() not in ('y', 'yes'):
        return CookieSetup(None, None)
    return CookieSetup(None, firefox_cookies(None, config, overrides, host,
                                             exporter=exporter, now=now))


def refresh_cookies(name: str, config: str | None = None,
                     overrides: Sequence[str] = (), host: Host = Host(),
                     *, exporter=None, now: float | None = None) -> dict:
    """Reread the stored origin; preserve default and reject stale refresh (§2)."""
    _cookie_name(name)
    root = settings.load(config, overrides, host)['paths.cookies']
    try:
        snapshot = cookie_writer.ensure_store(root)
        if name not in snapshot.sources:
            raise DownloadinuxError(f'{root / "sources.json"}: not registered; choose a listed source')
        expected = snapshot.sources[name]
        origin = {key: value for key, value in expected.record().items()
                  if key not in ('revision', 'refreshed_at')}
        material = _cookie_origin_material(origin, exporter)
        return _publish_cookie_report(root, name, material, origin, expected, now)
    except ConfigError:
        raise
    except DownloadinuxError as error:
        raise DownloadinuxError(f'source {name}: {error}') from None


def list_cookies(config: str | None = None, overrides: Sequence[str] = (),
                 host: Host = Host()) -> dict:
    """Host-only source metadata; migrate on first access (§2, §5, DEC-33)."""
    root = settings.load(config, overrides, host)['paths.cookies']
    return cookie_store.list_metadata(cookie_writer.ensure_store(root))


def inspect_cookies(name: str, config: str | None = None,
                    overrides: Sequence[str] = (), host: Host = Host(),
                    *, now: float | None = None) -> dict:
    """Shared secret-free source summary with an injectable clock (§2, DEC-30)."""
    _cookie_name(name)
    root = settings.load(config, overrides, host)['paths.cookies']
    snapshot = cookie_writer.ensure_store(root)
    return cookie_store.inspect(root, snapshot, name, now=now)


def cookie_default(name: str | None = None, *, anonymous: bool = False,
                   config: str | None = None, overrides: Sequence[str] = (),
                   host: Host = Host()) -> str | None:
    """Report or atomically select the source default (§1–§2, DEC-28),
    which the next `dl` or `batch` naming no source pins (§3)."""
    if name is not None:
        _cookie_name(name)
        if anonymous:
            raise ConfigError('source name and --anonymous conflict')
    root = settings.load(config, overrides, host)['paths.cookies']
    snapshot = cookie_writer.ensure_store(root)
    if name is not None or anonymous:
        snapshot = cookie_writer.set_default(root, name)
    return snapshot.default


def _invocation(effective: settings.Settings, host: Host) -> compose.Invocation:
    creds = credentials.load(effective.credentials_file)
    return compose.invocation(effective, creds, host.environ)


def run_compose(
    args: Sequence[str],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """Pass arguments through unchanged, preserving Docker's exit status."""
    effective = settings.load(config, overrides, host)
    return _invocation(effective, host).run(args, runner)


def shell(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """`shell`: interactive zsh as `dl`; its exit status (C03 list)."""
    effective = settings.load(config, overrides, host)
    return _invocation(effective, host).run(compose.exec_args(["zsh"]), runner)


def vpn(
    args: Sequence[str],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """`vpn`: the container's `vpn` helper with `args`, `status` when none.
    With the VPN off no `nordvpnd` runs, so it refuses and runs nothing
    (DEC-16)."""
    effective = settings.load(config, overrides, host)
    if not effective["vpn.enabled"]:
        raise DownloadinuxError(
            "vpn.enabled is false, so the container runs no NordVPN daemon; "
            "set vpn.enabled = true and run dlx restart to use it"
        )
    program = ["vpn", *(args or ["status"])]
    return _invocation(effective, host).run(compose.exec_args(program), runner)


# Named cookie sources §3, the host front (DEC-37): exact messages.
CLEANUP_PROGRAM = ("downloadinux-runner", "cleanup", "--state", "/state")
CLEANUP_NOT_RUNNING = (
    "the container is not running; start it with dlx up, then rerun dlx cookies cleanup"
)
CLEANUP_UNEXPECTED = "downloadinux-runner cleanup printed unexpected output"
CLEANUP_NO_RUNNER = (
    "the running container has no downloadinux-runner; rebuild and recreate it "
    "with dlx build and dlx restart"
)


@dataclass(frozen=True)
class CleanupCounts:
    removed: int
    skipped: int


def _cleanup_counts(output: str) -> CleanupCounts | None:
    """The runner's one JSON object, `{"removed": N, "skipped": M}` with
    non-negative integers (not booleans), or None for anything else. Any
    exception while parsing means the output was unexpected, whatever its
    type (Named cookie sources §3, the host front; DEC-37)."""
    def unique(pairs):
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(pairs)

    try:
        record = json.loads(output, object_pairs_hook=unique)
    except Exception:
        return None
    if type(record) is not dict or set(record) != {"removed", "skipped"}:
        return None
    if any(type(record[key]) is not int or record[key] < 0 for key in record):
        return None
    return CleanupCounts(record["removed"], record["skipped"])


def cookie_cleanup(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    capture: compose.Capture | None = None,
) -> CleanupCounts:
    """`cookies cleanup` (Named cookie sources §3, host front; DEC-37): ask
    the running container's runner to remove abandoned snapshots. Settings
    only: no registry read and no migration (§5). Standard error, the
    runner's and Compose's, stays on the terminal."""
    effective = settings.load(config, overrides, host)
    invocation = _invocation(effective, host)
    if not invocation.running(capture):
        raise DownloadinuxError(CLEANUP_NOT_RUNNING)
    status, output = invocation.capture(
        compose.exec_args(CLEANUP_PROGRAM, captured=True), capture
    )
    if status in (126, 127):  # Compose's "cannot run" and "not found"
        raise DownloadinuxError(CLEANUP_NO_RUNNER)
    if status != 0:
        raise DownloadinuxError(
            f"downloadinux-runner cleanup failed in the container (exit {status})"
        )
    counts = _cleanup_counts(output)
    if counts is None:
        raise DownloadinuxError(CLEANUP_UNEXPECTED)
    return counts


def list_servers(
    city: str = servers.DEFAULT_CITY,
    country: str = servers.DEFAULT_COUNTRY,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    capture: compose.Capture | None = None,
) -> list[str]:
    """`servers`: fetch Nord's recommendations in the container, which is
    the thing on the VPN, and rank them here. Runs with the VPN on or off
    (DEC-16)."""
    return servers.rank(_fetch_servers(country, config, overrides, host, capture), city)


def server_choices(
    city: str = servers.DEFAULT_CITY,
    country: str = servers.DEFAULT_COUNTRY,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
) -> servers.ServerChoices:
    """The same fetch as `servers`, returned as data (TUI §7; DEC-62)."""
    return servers.ranking(_fetch_servers(country, config, overrides, host, capture), city)


def _fetch_servers(country, config, overrides, host, capture) -> str:
    effective = settings.load(config, overrides, host)
    fetch = compose.exec_args(servers.fetch_argv(country), captured=True)
    status, body = _invocation(effective, host).capture(fetch, capture)
    if status != 0:
        raise DownloadinuxError(servers.UNREACHABLE)
    return body


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
    invocation = _invocation(effective, host)
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


def doctor(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    capture: compose.Capture | None = None,
) -> health.Result:
    """`doctor`: DEC-12's structured result, built after both configuration
    files load successfully. Individual probe failures remain checks (DEC-22)."""
    effective = settings.load(config, overrides, host)
    creds = credentials.load(effective.credentials_file)
    return health.inspect(effective, creds, host, capture)


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
    """`config set-token`'s first step: validate and store the NordVPN
    token. `log_in` follows it."""
    effective = settings.load(config, overrides, host)
    if not credentials.token_is_valid(token):
        raise DownloadinuxError(malformed_token_message(token))
    store_token(effective.credentials_file, token)
    return effective.credentials_file


class Login(str, Enum):
    VPN_OFF = "vpn-off"
    NOT_RUNNING = "not-running"
    LOGGED_IN = "logged-in"


@dataclass(frozen=True)
class Connect:
    server: tuple[str, ...]
    connected: bool


@dataclass(frozen=True)
class LoginResult:
    login: Login
    # Set when the login worked and vpn.autoconnect asked for a connect.
    connect: Connect | None = None


# Single-quoted for the container's sh: the token is expanded there, from the
# environment, and never appears in an argument list (DEC-16).
LOGIN_SCRIPT = 'nordvpn login --token "$NORDVPN_TOKEN"'
REFUSED = (
    "the token is stored, but the NordVPN daemon refused the login; a daemon "
    "already logged in needs dlx vpn logout --persist-token first, then set "
    "the token again"
)


def log_in(
    token: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    capture: compose.Capture | None = None,
) -> LoginResult:
    """`config set-token`'s second step (Host application, C03 list;
    DEC-16): with the VPN enabled and the container running, log the daemon
    in with `token`, which travels only in that `docker compose` process's
    environment, then connect when `vpn.autoconnect` is true. A refused login
    is an operation failure; a failed connect is reported, not raised. It
    never logs a daemon out."""
    effective = settings.load(config, overrides, host)
    if not effective["vpn.enabled"]:
        return LoginResult(Login.VPN_OFF)
    invocation = _invocation(effective, host)
    if not invocation.running(capture):
        return LoginResult(Login.NOT_RUNNING)
    login = compose.exec_args(
        ["sh", "-c", LOGIN_SCRIPT], captured=True, variables=["NORDVPN_TOKEN"]
    )
    # The login's output is discarded (DEC-17).
    status, _ = invocation.capture(login, capture, {"NORDVPN_TOKEN": token})
    if status != 0:
        raise DownloadinuxError(REFUSED)
    if not effective["vpn.autoconnect"]:
        return LoginResult(Login.LOGGED_IN)
    server = tuple(effective["vpn.server"].split())
    connect = compose.exec_args(["nordvpn", "connect", *server], captured=True)
    status, _ = invocation.capture(connect, capture)
    return LoginResult(Login.LOGGED_IN, Connect(server, status == 0))


USER_AGENT_KEY = "downloads.user_agent"


@dataclass(frozen=True)
class UserAgentResult:
    plist: Path
    settings_file: Path
    # Both None when Chrome's version could not be read: nothing was written.
    version: str | None = None
    major: str | None = None
    written: bool = False


def refresh_user_agent(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> UserAgentResult:
    """`config user-agent` (Host application, "Downloads, pinned for
    P0001-C04"; DEC-10, DEC-21): pin `downloads.user_agent` in the settings
    file to the host Chrome's major version. An unreadable version writes
    nothing; the CLI warns and exits 0. An existing file is edited in place
    (comments and permission bits kept) and left alone when it already holds
    the value; a missing one is written as `init`'s starter file."""
    effective = settings.load(config, overrides, host)  # rejects a bad file or --set
    plist = useragent.CHROME_PLIST
    result = UserAgentResult(plist, effective.file)
    version = useragent.chrome_version(plist)
    major = useragent.major(version) if version is not None else None
    if major is None:
        return result
    written = _write_setting(effective, overrides, host, USER_AGENT_KEY, useragent.user_agent(major))
    return UserAgentResult(plist, effective.file, version, major, written=written)


@dataclass(frozen=True)
class UserAgentWrite:
    settings_file: Path
    written: bool  # False: the file already held the value


def set_user_agent(
    value: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
) -> UserAgentWrite:
    """`config user-agent VALUE` (*Named cookie sources* §4, DEC-37): write an
    explicit `downloads.user_agent` literal through the same edit as the
    Chrome lookup, which it never reads. VALUE must be nonempty and a single
    line, else a usage error."""
    if not value or "\r" in value or "\n" in value:
        raise ConfigError(
            "config user-agent: VALUE must be nonempty and contain no CR or LF"
        )
    effective = settings.load(config, overrides, host)  # rejects a bad file or --set
    written = _write_setting(effective, overrides, host, USER_AGENT_KEY, value)
    return UserAgentWrite(effective.file, written)


@dataclass(frozen=True)
class SettingWrite:
    """A settings edit's raw TOML value, or None when removing the key."""

    settings_file: Path
    key: str
    value: Any
    written: bool


def set_setting(
    key: str, value: str, config: str | None = None,
    overrides: Sequence[str] = (), host: Host = Host(),
) -> SettingWrite:
    """Validate KEY, then VALUE, then the settings before writing the raw
    TOML value (Configuration and paths, Settings writes; DEC-61)."""
    setting = settings.BY_KEY.get(key)
    if setting is None:
        raise ConfigError(f"config set: {key}: unknown setting")
    raw = settings.override_value(setting, value)
    settings.validate(setting, raw, "config set")
    if setting.type in (settings.STRING, settings.PATH):
        if any(ch in raw for ch in ("\r", "\n", "\x00")) or (key == USER_AGENT_KEY and not raw):
            rule = "be nonempty and contain" if key == USER_AGENT_KEY else "contain"
            raise ConfigError(f"config set: {key}: VALUE must {rule} no CR, LF, or NUL")
    effective = settings.load(config, overrides, host)
    written = _write_setting(effective, overrides, host, key, raw)
    return SettingWrite(effective.file, key, raw, written)


def unset_setting(
    key: str, config: str | None = None, overrides: Sequence[str] = (),
    host: Host = Host(),
) -> SettingWrite:
    """Remove a setting so it falls back to its default (DEC-61).

    Configuration and paths, Settings writes: absent keys and files are
    no-ops; an existing file keeps its comments and permission bits.
    """
    if key not in settings.BY_KEY:
        raise ConfigError(f"config unset: {key}: unknown setting")
    effective = settings.load(config, overrides, host)
    path = effective.file
    written = False
    if effective.file_exists:
        text = files.unset_text(path.read_text(encoding="utf-8"), key)
        if text is not None:
            files.write_atomic(path, text, stat.S_IMODE(path.stat().st_mode))
            written = True
    return SettingWrite(path, key, None, written)


def _write_setting(
    effective: settings.Settings, overrides: Sequence[str], host: Host,
    key: str, value: Any,
) -> bool:
    """Set `key` to `value`: edit an existing file in place
    (comments and permission bits kept) unless it already holds the value, or
    write `init`'s starter file, `--set` values included and `value` winning.
    Returns whether the file was written."""
    path = effective.file
    if effective.file_exists:
        text = path.read_text(encoding="utf-8")
        table, _, name = key.partition(".")
        if tomllib.loads(text).get(table, {}).get(name) == value:
            return False
        files.write_atomic(
            path,
            files.setting_text(text, key, value),
            stat.S_IMODE(path.stat().st_mode),
        )
    else:
        values = {**settings.parse_overrides(overrides), key: value}
        try:
            files.write_atomic(
                path, files.starter_text(values, host), files.default_file_mode(),
                replace=False,
            )
        except FileExistsError:
            # Configuration and paths, Settings writes (DEC-61): the
            # starter write has init's no-clobber refusal, also for user-agent.
            raise DownloadinuxError(f"{path} already exists; not overwriting it") from None
    return True
