"""Captured host VPN operations (target design, Python + Textual TUI §7).

The TUI uses these operations; the CLI's `vpn` remains a pass-through.
Connection interpretation is shared with doctor and the worker (DEC-47),
and switching or disconnecting refuses running jobs (DEC-62).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from downloadinux import compose, host_jobs, host_worker, nordvpn_output, operations, settings
from downloadinux.errors import DownloadinuxError
from downloadinux.settings import Host


@dataclass(frozen=True)
class VpnState:
    enabled: bool
    running: bool
    connected: bool
    country: str | None
    server: str | None


@dataclass(frozen=True)
class VpnChange:
    status: int
    output: str


def _read_state(effective, host, capture) -> tuple[VpnState, compose.Invocation | None]:
    """One invocation and status read, reused by all three operations."""
    if not effective["vpn.enabled"]:
        return VpnState(False, False, False, None, None), None
    invocation = operations._invocation(effective, host)
    if not invocation.running(capture):
        return VpnState(True, False, False, None, None), invocation
    status, output = invocation.capture(compose.exec_args(["nordvpn", "status"], captured=True), capture)
    connected = nordvpn_output.connected(status, output)
    country = (nordvpn_output.value(output, "Country") or None) if connected else None
    server = (nordvpn_output.value(output, "Server") or None) if connected else None
    return VpnState(True, True, connected, country, server), invocation


def state(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
) -> VpnState:
    effective = settings.load(config, overrides, host)
    current, _ = _read_state(effective, host, capture)
    return current


def _guard(current, action, config, overrides, host, capture, feed) -> None:
    if not current.enabled:
        raise DownloadinuxError(operations.VPN_DISABLED)
    if not current.running:
        raise DownloadinuxError(host_worker.NOT_RUNNING)
    if current.connected:
        jobs = host_jobs.list_jobs(False, config, overrides, host, capture=capture, feed=feed)
        count = sum(job["state"] == "running" for job in jobs)
        if count:
            subject = "1 job is running; let it finish or cancel it" if count == 1 else (
                f"{count} jobs are running; let them finish or cancel them")
            raise DownloadinuxError(f"{subject} before {action}")


def connect(
    target: str,
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> VpnChange:
    effective = settings.load(config, overrides, host)
    current, invocation = _read_state(effective, host, capture)
    _guard(current, "switching servers", config, overrides, host, capture, feed)
    status, output = invocation.capture(
        compose.exec_args(["nordvpn", "connect", *target.split()], captured=True), capture)
    return VpnChange(status, output)


def disconnect(
    config: str | None = None,
    overrides: Sequence[str] = (),
    host: Host = Host(),
    *,
    capture: compose.Capture | None = None,
    feed: compose.Feed | None = None,
) -> VpnChange | None:
    effective = settings.load(config, overrides, host)
    current, invocation = _read_state(effective, host, capture)
    _guard(current, "disconnecting the VPN", config, overrides, host, capture, feed)
    if not current.connected:
        return None
    status, output = invocation.capture(compose.exec_args(["nordvpn", "disconnect"], captured=True), capture)
    return VpnChange(status, output)
