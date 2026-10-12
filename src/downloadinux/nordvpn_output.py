"""`nordvpn` output interpretation shared by doctor and the worker (DEC-47).

Target design *Health checks* (the `vpn.*` rows) and *Persistent worker and
jobs* §4, "VPN probe and retry clock". Standard library only, so the
container worker's VPN probe reads `nordvpn status` exactly as doctor's
`vpn.connected` check does without importing doctor's host dependencies.
"""
import re


def value(output: str, label: str) -> str | None:
    """The trimmed value of the first line whose label matches, ignoring case.

    Lines split at CR as well as LF, so a spinner frame ending in CR stays
    apart from the field after it. None when no line carries the label.
    """
    prefix = f"{label}:".casefold()
    for line in re.split(r"[\r\n]", output):
        line = line.strip()
        if line.casefold().startswith(prefix):
            return line[len(prefix):].strip()
    return None


def connected(status: int, output: str) -> bool:
    """`nordvpn status` reports a connection: exit status 0 and a first
    `Status` field of `Connected`, ignoring case."""
    return status == 0 and (value(output, "Status") or "").casefold() == "connected"
