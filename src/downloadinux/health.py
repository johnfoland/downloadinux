"""Structured health checks for ``doctor`` (DEC-12, DEC-22).

The result model is shared by the CLI and the later TUI.  Checks never carry
credentials or cookie values: only the fixed summaries from the target design.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux import (
    compose, cookie_store, cookie_writer, cookiejar, credentials, host_worker, nordvpn_output,
)
from downloadinux.errors import DownloadinuxError
from downloadinux.mounts import mounts
from downloadinux.settings import Host, Settings


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Check:
    id: str
    status: Status
    summary: str
    fix: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {"id": self.id, "status": self.status.value, "summary": self.summary}
        if self.fix is not None:
            value["fix"] = self.fix
        return value


@dataclass(frozen=True)
class Result:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not Status.FAIL for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.as_dict() for check in self.checks]}


def _check(
    id: str, status: Status, summary: str, fix: str | None = None
) -> Check:
    return Check(id, status, summary, fix)


def _config_file(settings: Settings) -> Check:
    if settings.file_exists:
        return _check("config.file", Status.OK, str(settings.file))
    return _check(
        "config.file",
        Status.WARN,
        f"{settings.file} missing; built-in defaults apply",
        "dlx init",
    )


def _credentials_file(creds: credentials.Credentials) -> Check:
    if not creds.file_exists:
        return _check("credentials.file", Status.SKIP, "no credentials file")
    try:
        mode = stat.S_IMODE(creds.file.stat().st_mode)
    except OSError as error:
        return _check(
            "credentials.file", Status.FAIL, f"{creds.file}: {error.strerror}"
        )
    rendered = f"{mode:04o}"
    if mode & 0o077 == 0:
        return _check(
            "credentials.file", Status.OK, f"{creds.file} (mode {rendered})"
        )
    return _check(
        "credentials.file",
        Status.FAIL,
        f"{creds.file} is mode {rendered}; it holds the NordVPN token",
        f"chmod 600 {creds.file}",
    )


def _credentials_token(creds: credentials.Credentials, vpn_enabled: bool) -> Check:
    state = creds.token_state
    if state is credentials.TokenState.SET:
        return _check(
            "credentials.token", Status.OK, "NordVPN token well-formed"
        )
    if state is credentials.TokenState.MALFORMED:
        return _check(
            "credentials.token",
            Status.FAIL,
            "NordVPN token is not 64 lowercase hex characters",
            "dlx config set-token",
        )
    if not vpn_enabled:
        return _check(
            "credentials.token", Status.SKIP, "VPN disabled in config"
        )
    return _check(
        "credentials.token",
        Status.WARN,
        "no NordVPN token",
        "dlx config set-token",
    )


def _downloads(path: Path) -> Check:
    if path.is_dir():
        return _check("paths.downloads", Status.OK, str(path))
    return _check(
        "paths.downloads",
        Status.FAIL,
        f"{path} does not exist (an unmounted drive?)",
        "create it, or set paths.downloads",
    )


# Named cookie sources §5, doctor's Stage 2 rows (DEC-39): exact texts.
_YOUTUBE_ASIDE = " (fine unless you need age-gated or members-only video)"
_STORE_FIX = "repair or restore the file it names"
_USER_AGENT_FIX = "dlx config user-agent VALUE"


def _site_rows(
    name: str, material: cookiejar.Material, now: float
) -> tuple[Check, Check]:
    """Present-and-unexpired is ok; only expired ones, or none, warn. A record
    with no expiry is never expired."""
    expired: dict[str, bool] = {}
    for cookie in material.jar:
        site = cookiejar.session_site(cookie.domain, cookie.name)
        if site is not None:
            stale = cookie.expires is not None and cookie.expires <= now
            expired[site] = expired.get(site, True) and stale
    fix = f"dlx cookies refresh {name}"
    rows = []
    for site, aside, site_fix in (
        ("instagram", "", f"log in to Instagram, then: {fix}"),
        ("youtube", _YOUTUBE_ASIDE, None),
    ):
        id = f"cookies.{site}"
        if site not in expired:
            rows.append(_check(id, Status.WARN, f"{site}: no session cookie{aside}", site_fix))
        elif expired[site]:
            rows.append(_check(id, Status.WARN, f"{site}: session cookie expired{aside}", site_fix))
        else:
            rows.append(_check(id, Status.OK, f"{site}: session cookie present"))
    return rows[0], rows[1]


def _source_user_agent(value: str) -> Check:
    """Presence and CR/LF only; no Chrome version is read (§4, §5)."""
    if not value:
        return _check(
            "downloads.user_agent", Status.WARN, "user-agent is empty", _USER_AGENT_FIX
        )
    if "\r" in value or "\n" in value:
        return _check(
            "downloads.user_agent",
            Status.WARN,
            "user-agent contains CR or LF; downloads refuse it",
            _USER_AGENT_FIX,
        )
    return _check("downloads.user_agent", Status.OK, "explicit user-agent set")


def source_checks(
    cookie_root: Path, user_agent: str, *, now: float | None = None
) -> tuple[Check, Check, Check, Check]:
    """Doctor's Stage 2 `cookies.jar`, `cookies.instagram`, `cookies.youtube`
    and `downloads.user_agent` rows, in that order (Named cookie sources §5,
    DEC-39). Migrates as any source operation does, then reads the default's
    current revision through the shared reader. Every store failure is the
    `cookies.jar` failure, never an exception, so the report continues."""
    now = time.time() if now is None else now
    agent = _source_user_agent(user_agent)
    try:
        snapshot = cookie_writer.ensure_store(cookie_root)
        name = snapshot.default
        material = None if name is None else cookie_store.load_revision(
            cookie_root, name, snapshot.sources[name].revision
        )
    except (DownloadinuxError, OSError) as error:
        if isinstance(error, OSError):
            path = cookie_root if error.filename is None else error.filename
            summary = f"{path}: {error.strerror or error}"
        else:
            summary = str(error)
        return (
            _check("cookies.jar", Status.FAIL, summary, _STORE_FIX),
            _check("cookies.instagram", Status.SKIP, "no usable cookie source"),
            _check("cookies.youtube", Status.SKIP, "no usable cookie source"),
            agent,
        )
    if name is None or material is None:
        return (
            _check("cookies.jar", Status.SKIP, "anonymous cookie default"),
            _check("cookies.instagram", Status.SKIP, "anonymous cookie default"),
            _check("cookies.youtube", Status.SKIP, "anonymous cookie default"),
            agent,
        )
    counts = cookiejar.summary(material, now=now)
    if not counts["total"]:
        return (
            _check(
                "cookies.jar",
                Status.WARN,
                f"source {name} has no cookies; public downloads still work",
                f"dlx cookies refresh {name}",
            ),
            _check("cookies.instagram", Status.SKIP, "no usable cookie source"),
            _check("cookies.youtube", Status.SKIP, "no usable cookie source"),
            agent,
        )
    jar = _check(
        "cookies.jar",
        Status.OK,
        f"source {name}: {counts['total']} cookies ({counts['expired']} expired)",
    )
    return (jar, *_site_rows(name, material, now), agent)


def _process_environment(host: Host) -> dict[str, str]:
    source: Mapping[str, str] = os.environ if host.environ is None else host.environ
    return {key: value for key, value in source.items() if not key.startswith("DOWNLOADINUX_")}


def _container_capture(
    invocation: compose.Invocation,
    program: list[str],
    capture: compose.Capture,
) -> tuple[int, str]:
    try:
        return invocation.capture(
            compose.exec_args(program, captured=True), capture
        )
    except OSError:
        return 1, ""


def _worker_running(
    invocation: compose.Invocation | None,
    capture: compose.Capture,
    docker: Check,
    running: Check,
) -> Check:
    """`worker.running` (Persistent worker and jobs §8; DEC-22 as amended,
    DEC-53): `downloadinux-worker status` as a container check, its output
    judged by the host's shared reply rule, with §8's texts."""
    if docker.status is not Status.OK:
        return _check("worker.running", Status.SKIP, "Docker daemon not reachable")
    if running.status is not Status.OK or invocation is None:
        return _check("worker.running", Status.SKIP, "container not running")
    status, output = _container_capture(
        invocation, [host_worker.PROGRAM, "status"], capture
    )
    if status in (126, 127):
        return _check(
            "worker.running", Status.FAIL, "the running image has no worker",
            "dlx build, then dlx restart",
        )
    reply = host_worker.reply(output) if status == 0 else None
    counts = None if reply is None or "error" in reply else (reply.get("running"), reply.get("queued"))
    if counts is not None and all(type(count) is int and count >= 0 for count in counts):
        return _check(
            "worker.running", Status.OK, f"worker running ({counts[0]} running, {counts[1]} queued)"
        )
    return _check("worker.running", Status.FAIL, "worker not answering", "dlx restart")


def _container_checks(
    settings: Settings,
    invocation: compose.Invocation | None,
    capture: compose.Capture,
    running: Check,
) -> list[Check]:
    ids = [
        "mount.downloads.mounted", "mount.downloads.writable",
        "mount.config.mounted", "mount.config.readonly",
        "mount.state.mounted", "mount.state.writable",
        "mount.cookies.mounted", "mount.cookies.writable",
        "tools.yt-dlp", "tools.gallery-dl",
    ]
    if running.status is not Status.OK or invocation is None:
        return [_check(id, Status.SKIP, "container not running") for id in ids]

    checks = []
    uid = settings["container.uid"]
    for mount in mounts(settings):
        name = mount.container.removeprefix("/")
        status, _ = _container_capture(
            invocation, ["mountpoint", "-q", mount.container], capture
        )
        mounted = _check(
            f"mount.{name}.mounted",
            Status.OK if status == 0 else Status.FAIL,
            (
                f"{mount.container} is a mount point"
                if status == 0
                else f"{mount.container} is not a mount point"
            ),
            None if status == 0 else "dlx restart",
        )
        checks.append(mounted)

        # DEC-22, amended: with no mount at the path, `test -w` fails and a
        # write lands in the container's own filesystem, so either probe would
        # pass for the wrong reason. Neither runs.
        if mounted.status is not Status.OK:
            probe = "readonly" if mount.access == "ro" else "writable"
            checks.append(
                _check(
                    f"mount.{name}.{probe}",
                    Status.SKIP,
                    f"{mount.container} is not a mount point",
                )
            )
            continue

        if mount.access == "ro":
            status, _ = _container_capture(
                invocation, ["test", "-w", mount.container], capture
            )
            checks.append(
                _check(
                    f"mount.{name}.readonly",
                    Status.OK if status != 0 else Status.FAIL,
                    (
                        f"{mount.container} is read-only"
                        if status != 0
                        else f"{mount.container} is writable"
                    ),
                    None if status != 0 else "dlx restart",
                )
            )
            continue

        status, _ = _container_capture(
            invocation,
            [
                "sh", "-c",
                'f=$(mktemp -p "$1" .downloadinux-doctor.XXXXXX 2>/dev/null) && rm -f "$f"',
                "sh", mount.container,
            ],
            capture,
        )
        checks.append(
            _check(
                f"mount.{name}.writable",
                Status.OK if status == 0 else Status.FAIL,
                (
                    f"dl (uid {uid}) can write {mount.container}"
                    if status == 0
                    else f"dl (uid {uid}) cannot write {mount.container}"
                ),
                None if status == 0 else f"make {mount.host} writable by uid {uid}",
            )
        )

    for tool in ("yt-dlp", "gallery-dl"):
        status, output = _container_capture(
            invocation, [tool, "--version"], capture
        )
        version = output.splitlines()[0].strip() if output.splitlines() else ""
        worked = status == 0 and bool(version)
        checks.append(
            _check(
                f"tools.{tool}",
                Status.OK if worked else Status.FAIL,
                f"{tool} {version}" if worked else f"{tool} did not run",
                None if worked else "dlx update",
            )
        )
    return checks


def _vpn_checks(
    settings: Settings,
    invocation: compose.Invocation | None,
    capture: compose.Capture,
    running: Check,
) -> list[Check]:
    ids = ["vpn.login", "vpn.connected", "vpn.killswitch", "vpn.exit_ip"]
    if not settings["vpn.enabled"]:
        return [_check(id, Status.SKIP, "VPN disabled in config") for id in ids]
    if running.status is not Status.OK or invocation is None:
        return [_check(id, Status.SKIP, "container not running") for id in ids]

    status, _ = _container_capture(
        invocation, ["nordvpn", "account"], capture
    )
    login = _check(
        "vpn.login",
        Status.OK if status == 0 else Status.FAIL,
        (
            "NordVPN daemon logged in"
            if status == 0
            else "NordVPN daemon not logged in (token missing, invalid, or revoked)"
        ),
        None if status == 0 else "dlx config set-token",
    )

    status, output = _container_capture(
        invocation, ["nordvpn", "status"], capture
    )
    # The worker's VPN gate reads the same interpretation (DEC-47).
    connected = nordvpn_output.connected(status, output)
    if connected:
        location = [
            value
            for value in (
                nordvpn_output.value(output, "Country"),
                nordvpn_output.value(output, "Server"),
            )
            if value
        ]
        summary = "connected" + (f": {', '.join(location)}" if location else "")
        connection = _check("vpn.connected", Status.OK, summary)
    else:
        connection = _check(
            "vpn.connected", Status.FAIL, "VPN not connected", "dlx vpn on"
        )

    status, output = _container_capture(
        invocation, ["nordvpn", "settings"], capture
    )
    observed = nordvpn_output.value(output, "Kill Switch") if status == 0 else None
    observed = observed.casefold() if observed is not None else None
    if observed not in {"enabled", "disabled"}:
        observed = None
        killswitch = _check(
            "vpn.killswitch", Status.WARN, "killswitch state unknown"
        )
    else:
        expected = "enabled" if settings["vpn.killswitch"] else "disabled"
        if observed == expected:
            killswitch = _check(
                "vpn.killswitch",
                Status.OK,
                f"killswitch {observed}, as configured",
            )
        else:
            configured = str(settings["vpn.killswitch"]).lower()
            killswitch = _check(
                "vpn.killswitch",
                Status.WARN,
                f"killswitch {observed}, but vpn.killswitch is {configured}",
                "dlx restart",
            )

    status, output = _container_capture(
        invocation,
        ["curl", "-s", "--max-time", "8", "https://ipinfo.io/json"],
        capture,
    )
    data: object = None
    if status == 0:
        try:
            data = json.loads(output)
        except Exception:
            # DEC-22 and review-checklist §3: malformed input is a failed
            # check regardless of the parser's exception taxonomy.
            pass
    if isinstance(data, dict) and isinstance(data.get("ip"), str):
        ip = data["ip"]
        city, country = data.get("city"), data.get("country")
        where = f" ({city}, {country})" if isinstance(city, str) and isinstance(country, str) else ""
        exit_ip = _check("vpn.exit_ip", Status.OK, f"exit IP {ip}{where}")
    elif observed == "enabled" and not connected:
        exit_ip = _check(
            "vpn.exit_ip",
            Status.FAIL,
            "no internet: the killswitch is blocking traffic while the VPN is disconnected",
            "dlx vpn on",
        )
    else:
        exit_ip = _check(
            "vpn.exit_ip", Status.FAIL, "no internet from the container"
        )
    return [login, connection, killswitch, exit_ip]


def inspect(
    settings: Settings,
    creds: credentials.Credentials,
    host: Host = Host(),
    capture: compose.Capture | None = None,
) -> Result:
    """Run the host portion of DEC-22's fixed list, in its pinned order."""
    checks = [
        _config_file(settings),
        _credentials_file(creds),
        _credentials_token(creds, settings["vpn.enabled"]),
        _downloads(settings["paths.downloads"]),
    ]
    # Named cookie sources §5, doctor's Stage 2 rows (DEC-39), at the Stage 1
    # rows' IDs and positions.
    checks += source_checks(settings["paths.cookies"], settings["downloads.user_agent"])

    run_capture = capture or compose.capture_process
    try:
        docker_status, version = run_capture(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            _process_environment(host),
        )
    except OSError as error:
        docker = _check("docker.daemon", Status.FAIL, f"docker: {error.strerror}")
    else:
        if docker_status == 0:
            docker = _check(
                "docker.daemon", Status.OK, f"Docker {version.strip()}"
            )
        else:
            docker = _check(
                "docker.daemon",
                Status.FAIL,
                "Docker daemon not reachable",
                "start Docker",
            )
    checks.append(docker)

    invocation = None
    if docker.status is not Status.OK:
        running_check = _check(
            "container.running", Status.SKIP, "Docker daemon not reachable"
        )
    else:
        invocation = compose.invocation(settings, creds, host.environ)
        try:
            running = invocation.running(run_capture)
        except OSError:
            running = False
        running_check = _check(
            "container.running",
            Status.OK if running else Status.FAIL,
            (
                f"container running (project {settings['container.project']})"
                if running
                else "container not running"
            ),
            None if running else "dlx up",
        )
    checks.append(running_check)
    checks.append(_worker_running(invocation, run_capture, docker, running_check))
    checks += _container_checks(settings, invocation, run_capture, running_check)
    checks += _vpn_checks(settings, invocation, run_capture, running_check)
    return Result(tuple(checks))
