"""The worker's scheduler: VPN probe and throttle test seam (DEC-47).

Target design, *Persistent worker and jobs* §4, "Scheduler passes, VPN probe,
and throttle test seam". Standard library only (§1, DEC-41): no settings,
cookie material, host paths, or doctor dependencies.
"""
from collections.abc import Mapping
import subprocess
from typing import Callable

from . import nordvpn_output
from .errors import DownloadinuxError

PROBE_ARGV = ('nordvpn', 'status')
PROBE_TIMEOUT = 5  # seconds (§4 *VPN probe and retry clock*)
THROTTLE_VARIABLE = 'DOWNLOADINUX_TEST_THROTTLE_SECONDS'
THROTTLE_MAX = 289  # the upper bound of the store's default draw (§4 *Throttle*)
THROTTLE_ERROR = f'invalid {THROTTLE_VARIABLE}; expected an integer from 0 to {THROTTLE_MAX}'


def probe_vpn() -> bool:
    """Whether `nordvpn status` reports a connection, as doctor reads it.

    No shell, `/dev/null` input, stdout captured and decoded as UTF-8 with
    replacement, stderr discarded, five-second timeout. A failure to run, a
    timeout, a nonzero status, or an absent, disconnected, or unrecognized
    status is not ready. Output and exception text are never printed,
    stored, or returned.
    """
    try:
        completed = subprocess.run(
            list(PROBE_ARGV), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return nordvpn_output.connected(completed.returncode, completed.stdout.decode('utf-8', 'replace'))


def throttle_override(environ: Mapping[str, str]) -> Callable[[], int] | None:
    """The test throttle seam read from an explicit environment mapping.

    Absent: None, so the store keeps its uniform 120–289-second draw.
    Present: a nonempty string of ASCII digits from 0 to 289 inclusive, any
    number of leading zeros allowed, giving a constant delay callable for
    `open_store(delay=...)`, which changes future draws only. Anything else
    raises `DownloadinuxError` without echoing the value. The service reads
    this before opening the store; importing this module reads nothing.
    """
    if THROTTLE_VARIABLE not in environ:
        return None
    raw = environ[THROTTLE_VARIABLE]
    if type(raw) is not str or not raw or any(char not in '0123456789' for char in raw):
        raise DownloadinuxError(THROTTLE_ERROR)
    # Strip zeros before int(), so arbitrarily long spellings never reach
    # its digit limit and long out-of-range values fail by length.
    digits = raw.lstrip('0') or '0'
    if len(digits) > len(str(THROTTLE_MAX)) or int(digits) > THROTTLE_MAX:
        raise DownloadinuxError(THROTTLE_ERROR)
    seconds = int(digits)
    return lambda: seconds
