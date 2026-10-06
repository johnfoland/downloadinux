"""Structured health checks for ``doctor`` (DEC-12, DEC-22).

The result model is shared by the CLI and the later TUI.  Checks never carry
credentials or cookie values: only the fixed summaries from the target design.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from downloadinux import compose, credentials, useragent
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


@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str


_CHROME = re.compile(r"Chrome/([0-9]+)")


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


def _read_jar(path: Path) -> tuple[Check, tuple[Cookie, ...]]:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return (
            _check(
                "cookies.jar",
                Status.WARN,
                "no cookies yet; public downloads still work",
                "just cookies",
            ),
            (),
        )
    except OSError as error:
        return _check("cookies.jar", Status.FAIL, f"{path}: {error.strerror}"), ()

    cookies = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            cookies.append(Cookie(fields[0], fields[5]))
    if not cookies:
        return (
            _check(
                "cookies.jar",
                Status.WARN,
                "no cookies yet; public downloads still work",
                "just cookies",
            ),
            (),
        )
    return (
        _check("cookies.jar", Status.OK, f"{path}: {len(cookies)} cookies"),
        tuple(cookies),
    )


def _belongs_to(domain: str, site: str) -> bool:
    domain = domain.removeprefix(".").lower()
    return domain == site or domain.endswith(f".{site}")


def _site_cookie(
    id: str,
    label: str,
    site: str,
    names: tuple[str, ...],
    cookies: tuple[Cookie, ...],
    jar: Check,
    missing: str,
    fix: str | None = None,
) -> Check:
    if jar.status is not Status.OK:
        return _check(id, Status.SKIP, "no cookie jar")
    present = any(
        cookie.name in names and _belongs_to(cookie.domain, site)
        for cookie in cookies
    )
    if present:
        return _check(id, Status.OK, f"{label}: session cookie present")
    return _check(id, Status.WARN, missing, fix)


def _user_agent(value: str) -> Check:
    configured = _CHROME.search(value)
    if configured is None:
        return _check(
            "downloads.user_agent",
            Status.WARN,
            "user-agent is not Chrome's; Instagram caps video at 720p",
            "dlx config user-agent",
        )
    configured_major = configured.group(1)
    version = useragent.chrome_version(useragent.CHROME_PLIST)
    host_major = useragent.major(version) if version is not None else None
    if host_major is None or host_major == configured_major:
        return _check(
            "downloads.user_agent", Status.OK, f"Chrome {configured_major}"
        )
    return _check(
        "downloads.user_agent",
        Status.WARN,
        f"user-agent says Chrome {configured_major}, host Chrome is {host_major}",
        "dlx config user-agent",
    )


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

        if mounted.status is not Status.OK:
            checks.append(
                _check(
                    f"mount.{name}.writable",
                    Status.SKIP,
                    f"{mount.container} is not a mount point",
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
    jar, cookies = _read_jar(settings["paths.cookies"] / "cookies.txt")
    checks += [
        jar,
        _site_cookie(
            "cookies.instagram",
            "instagram",
            "instagram.com",
            ("sessionid",),
            cookies,
            jar,
            "instagram: no session cookie",
            "log in to Instagram in Firefox, then: just cookies",
        ),
        _site_cookie(
            "cookies.youtube",
            "youtube",
            "youtube.com",
            ("__Secure-3PSID", "SAPISID"),
            cookies,
            jar,
            "youtube: no session cookie (fine unless you need age-gated or members-only video)",
        ),
        _user_agent(settings["downloads.user_agent"]),
    ]

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
    checks += _container_checks(settings, invocation, run_capture, running_check)
    return Result(tuple(checks))
