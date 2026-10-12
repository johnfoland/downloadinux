"""The worker's scheduler: passes, VPN gate, and throttle test seam (DEC-47).

Target design, *Persistent worker and jobs* §4, "Scheduler passes, VPN probe,
and throttle test seam". Standard library only (§1, DEC-41): no settings,
cookie material, host paths, or doctor dependencies. It drives the owned job
store through its public operations only; the service decides when to run a
pass, and launches, polls, signals, and waits for runners itself.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import subprocess
import time
from typing import Callable

from . import job_store, nordvpn_output
from .errors import DownloadinuxError

PROBE_ARGV = ('nordvpn', 'status')
PROBE_TIMEOUT = 5  # seconds (§4 *VPN probe and retry clock*)
VPN_RETRY_SECONDS = 15  # after a failed probe completes (§4, DEC-43)
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _queue_key(job: job_store.JobRecord) -> tuple[str | None, int | None]:
    """The (lane, submission) pair `JobStore.queue_heads` names a queue by."""
    return (job.lane, None) if job.lane is not None else (None, job.submission)


class Scheduler:
    """Scheduling passes over an owned `JobStore` (§4, DEC-47).

    `clock` must be the aware UTC wall clock the store was opened with, so a
    lane deadline is compared as the store's start compares it; `monotonic`
    times the VPN retry interval, which wall-clock adjustments cannot
    shorten. With `vpn` false the probe is never called; otherwise `probe`
    returns True only when the VPN is connected (`probe_vpn` by default).

    `launch` receives each started job's committed running record, launches
    and registers its runner, and returns without waiting for it to exit. A
    `DownloadinuxError` from it is an expected launch failure: its message
    must be a nonempty, NUL-free reason naming no URL, user-agent, or cookie
    value, and becomes the failed job's `error`. Any other exception, or a
    reason that breaks that contract, propagates with the job left running
    for reconciliation (§5).

    Calls are serial on the store's owning thread. The VPN retry deadline is
    held by the instance only, so it resets with a new scheduler.
    """

    def __init__(self, store: job_store.JobStore, launch: Callable[[job_store.JobRecord], None], *,
                 vpn: bool, probe: Callable[[], bool] = probe_vpn,
                 clock: Callable[[], datetime] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic):
        if type(vpn) is not bool:
            raise DownloadinuxError('scheduler VPN mode must be a boolean')
        self._store = store
        self._launch = launch
        self._vpn = vpn
        self._probe = probe
        self._clock = clock
        self._monotonic = monotonic
        self._retry_at: float | None = None
        self._stopping = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    def stop(self) -> None:
        """Start nothing more and call back no more, in this pass or later.

        One way. It neither cancels nor marks jobs: a job whose start already
        committed stays running, unlaunched, for shutdown's mark (§5).
        """
        self._stopping = True

    def run_pass(self) -> tuple[int, ...]:
        """Consider every queue once, in the store's queue-head order.

        Returns the IDs whose launch callback returned normally, in visit
        order. Each queue starts at most its head; there is no global cap,
        and nothing sleeps or waits for a runner. Jobs behind a head, and
        heads of queues with a running job, lose any wait; an idle head
        waits for its lane deadline when that is still ahead, else for the
        VPN when the gate is closed, and otherwise starts. A wait is written
        only when it changes. Store errors propagate (a failed start reaches
        no callback), as do unexpected callback exceptions.
        """
        if self._stopping:
            return ()
        heads = self._store.queue_heads()
        head_ids = {entry.head.id for entry in heads}
        behind: dict[tuple[str | None, int | None], list[int]] = {}
        for job in self._store.list(state='queued'):
            if job.id not in head_ids and job.wait is not None:
                behind.setdefault(_queue_key(job), []).append(job.id)
        launched = []
        for entry in heads:
            if self._stopping:
                break
            for job_id in behind.get((entry.lane, entry.submission), ()):
                self._store.set_wait(job_id, None)
            head = entry.head
            if entry.running:
                self._wait(head, None)
                continue
            # Throttle first: a future deadline skips the probe. A deadline
            # equal to now is due, as the store's start agrees.
            if entry.next_start_at is not None and entry.next_start_at > self._now():
                self._wait(head, 'throttle', {'reason': 'throttle', 'until': entry.next_start_at})
                continue
            if self._vpn:
                ready = self._vpn_ready()
                if self._stopping:
                    break
                if not ready:
                    self._wait(head, 'vpn', {'reason': 'vpn'})
                    continue
            record = self._store.start(head.id)
            if self._stopping:
                break  # committed, never launched: shutdown marks it (§5)
            try:
                self._launch(record)
            except DownloadinuxError as error:
                reason = str(error)
                if not reason or '\0' in reason:
                    raise
                # Draws the lane deadline; this queue's next job waits for a
                # later pass (§5 *Lane deadline*).
                self._store.end(record.id, launch_failure=reason)
                continue
            launched.append(record.id)
        return tuple(launched)

    def _vpn_ready(self) -> bool:
        """Probe unless a failure's retry deadline is still ahead.

        Only failures are remembered: a success clears the deadline and is
        never reused, so each eligible start probes afresh (DEC-47).
        """
        if self._retry_at is not None and self._monotonic() < self._retry_at:
            return False
        ready = self._probe() is True
        self._retry_at = None if ready else self._monotonic() + VPN_RETRY_SECONDS
        return ready

    def _wait(self, job: job_store.JobRecord, reason: str | None, wait: dict | None = None) -> None:
        if job.wait != wait:
            self._store.set_wait(job.id, reason)

    def _now(self) -> str:
        instant = self._clock()
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise DownloadinuxError('scheduler clock must return a timezone-aware time')
        return instant.astimezone(timezone.utc).strftime(job_store.TIMESTAMP_FORMAT)
