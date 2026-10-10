"""Operations behind the CLI's subcommands, callable without argparse.

Each takes arguments and returns data or raises a `DownloadinuxError`; the
CLI (and later the TUI) only parses and presents.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux import compose, cookie_selectors, cookie_store, cookie_writer, credentials, files, health, presets, servers, settings, useragent
from downloadinux.errors import ConfigError, DownloadinuxError
from downloadinux.mounts import mounts
from downloadinux.settings import Host


def _cookie_name(name: str) -> None:
    if type(name) is not str or not cookie_store.NAME.fullmatch(name):
        raise ConfigError('source name must match [a-z0-9][a-z0-9_-]{0,63}')


def cookie_browsers(*, platform: str | None = None) -> dict:
    """Capabilities only; no installation probe, settings, or migration (§2)."""
    return cookie_selectors.support(platform=platform)


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
    """Report or atomically select the future source default (§1–§2, DEC-28).

    Existing Stage 1 download/export/setup/doctor routes stay on their shared
    jar until the coordinated cutover described in §5.
    """
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


def download(
    preset: str,
    urls: Sequence[str],
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """`dl`: the preset's argument list (*Presets*) with `urls` appended, run
    as `dl` uncaptured, since the downloaders draw progress on the terminal;
    the downloader's exit status. No VPN or running check: Compose reports a
    stopped service itself (Host application, "Downloads, pinned for
    P0001-C04")."""
    effective = settings.load(config, overrides, host)
    program = [
        *presets.argv(presets.resolve(preset).name, effective["downloads.user_agent"]),
        *urls,
    ]
    return _invocation(effective, host).run(compose.exec_args(program), runner)


# `zsh -f` reads none of dl's startup files: only the loop's own file
# (Host application, "Downloads, pinned for P0001-C04"; DEC-13).
BATCH_SCRIPT = (
    'source /home/dl/.zsh-custom/batch.zsh && _batch_downloader_core "$@"; '
    'echo; echo "[done — press a key]"; read -k1'
)


@dataclass(frozen=True)
class BatchResult:
    queue: str  # the container's path
    session: str
    preset: str


def batch(
    preset: str,
    queue: str | None = None,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> BatchResult:
    """`batch`: start the zsh batch loop on a queue under `paths.downloads`
    in a detached tmux session, with the preset's batch argument list as
    separate tmux arguments, so every word arrives intact (Host application,
    "Downloads, pinned for P0001-C04"). Paths are made absolute without
    resolving links, so a symlinked drive compares as written."""
    effective = settings.load(config, overrides, host)
    chosen = presets.resolve(preset)
    downloads = os.path.abspath(effective["paths.downloads"])
    if queue is None:
        path = os.path.join(downloads, chosen.queue)
    else:
        path = os.path.abspath(os.path.expanduser(queue))
    if not os.path.isfile(path):
        raise DownloadinuxError(f"no such queue file: {path}")
    if path == downloads or os.path.commonpath([downloads, path]) != downloads:
        raise DownloadinuxError(
            f"{path} is outside paths.downloads ({downloads}); the container cannot see it"
        )
    container = "/downloads/" + os.path.relpath(path, downloads)
    program = [
        "tmux", "new-session", "-d", "-s", chosen.session, "-c", "/downloads",
        "zsh", "-f", "-c", BATCH_SCRIPT, "zsh",
        container, chosen.log_prefix, chosen.title, chosen.colour,
        *presets.argv(chosen.name, effective["downloads.user_agent"], batch=True),
    ]
    status = _invocation(effective, host).run(compose.exec_args(program), runner)
    if status != 0:
        raise DownloadinuxError(
            f"could not start tmux session {chosen.session}; if one is already "
            f"running, attach with dlx watch {chosen.name}"
        )
    return BatchResult(container, chosen.session, chosen.name)


def watch(
    target: str = presets.VIDEO,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    runner: compose.Runner | None = None,
) -> int:
    """`watch`: attach to a preset's batch session, or to any other word
    taken as a tmux session name; tmux's exit status."""
    effective = settings.load(config, overrides, host)
    session = presets.resolve(target).session if target in presets.ALIASES else target
    program = ["tmux", "attach", "-t", session]
    return _invocation(effective, host).run(compose.exec_args(program), runner)


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
    effective = settings.load(config, overrides, host)
    fetch = compose.exec_args(servers.fetch_argv(country), captured=True)
    status, body = _invocation(effective, host).capture(fetch, capture)
    if status != 0:
        raise DownloadinuxError(servers.UNREACHABLE)
    return servers.rank(body, city)


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
    value = useragent.user_agent(major)
    path = effective.file
    if effective.file_exists:
        text = path.read_text(encoding="utf-8")
        if tomllib.loads(text).get("downloads", {}).get("user_agent") == value:
            return UserAgentResult(plist, path, version, major, written=False)
        files.write_atomic(
            path,
            files.setting_text(text, USER_AGENT_KEY, value),
            stat.S_IMODE(path.stat().st_mode),
        )
    else:
        values = {**settings.parse_overrides(overrides), USER_AGENT_KEY: value}
        files.write_atomic(
            path, files.starter_text(values, host), files.default_file_mode(),
            replace=False,
        )
    return UserAgentResult(plist, path, version, major, written=True)
