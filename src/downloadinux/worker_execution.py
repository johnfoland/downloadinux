"""The worker's runner execution: launch, logs, exits, cancel, and shutdown (DEC-48).

Target design, *Persistent worker and jobs* §4 *Execution* and §5, "Runner
execution, cancel signals, and the shutdown wait". Standard library only (§1,
DEC-41): no settings, cookie material, or host paths. It uses the owned job
store's public operations and gives the scheduler its launch callback
(DEC-47); the service decides when to poll. Calls are serial on the store's
owning thread.
"""
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import Callable

from . import job_store
from .errors import DownloadinuxError

RUNNER = 'downloadinux-runner'
LOG_FLAGS = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
             | os.O_CLOEXEC | os.O_NONBLOCK)  # a planted FIFO cannot stall a launch
LAUNCH_FAILED = 'launch failed'
STOPPED = 'the worker has stopped'
SHUTDOWN_WAIT = 20  # seconds after the shutdown signals (§5 *Shutdown*)
POLL_INTERVAL = 0.1  # the longest sleep between shutdown polls


def runner_argv(record: job_store.JobRecord, cookies: Path, state: Path) -> list[str]:
    """`downloadinux-runner download --cookies COOKIES --state STATE SELECTION
    --user-agent UA PRESET URL`, each item one element (§5 *Roots and
    argument list*). SELECTION is the record's pinned pair, or `--anonymous`
    when both are null; the user-agent is verbatim, possibly empty.
    """
    if record.source is None:
        selection = ['--anonymous']
    else:
        selection = ['--source', record.source, '--revision', record.revision]
    return [RUNNER, 'download', '--cookies', str(cookies), '--state', str(state), *selection,
            '--user-agent', record.user_agent, record.preset, record.url]


def _reason(error: OSError, path: object) -> str:
    """`<path>: <OS reason>`, or `launch failed` when the OS gives no reason (DEC-48)."""
    return f'{path}: {error.strerror}' if error.strerror else LAUNCH_FAILED


class Executor:
    """Runs started jobs' runners for an owned `JobStore` (§5, DEC-48).

    `cookies`, `state`, and `downloads` are the roots the service was started
    with (`/cookies`, `/state`, `/downloads` in the image); there are no
    defaults. `monotonic` and `sleep` time the shutdown wait.

    Runners are registered by job ID from the moment their process exists
    until the store has committed their end.
    """

    def __init__(self, store: job_store.JobStore, *, cookies: Path, state: Path, downloads: Path,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._store = store
        self._cookies = Path(cookies)
        self._state = Path(state)
        self._downloads = Path(downloads)
        self._monotonic = monotonic
        self._sleep = sleep
        self._running: dict[int, subprocess.Popen] = {}
        self._stopping = False  # shutdown has begun: every end passes stopping
        self._stopped = False  # shutdown has returned: every call is refused

    @property
    def running(self) -> tuple[int, ...]:
        """The registered runners' job IDs, in ID order."""
        return tuple(sorted(self._running))

    def launch(self, record: job_store.JobRecord) -> None:
        """The scheduler's launch callback: start one attempt's runner and return.

        Opens the attempt's log (§2's `<id>-<attempt>.log`) without following
        a symlink or blocking, requires a regular file at 0600, then starts
        the runner in its own process group in the download root, with
        `/dev/null` input, the log as standard output and error, and no other
        descriptor. The worker writes nothing into the log. Failures before
        the process exists raise `DownloadinuxError` with DEC-48's reasons,
        which the scheduler records as the job's `error`; the log stays.
        """
        self._refuse_stopped()
        log = self._store.logs / job_store.log_name(record.id, record.attempt)
        fd = self._open_log(log)
        try:
            process = subprocess.Popen(
                runner_argv(record, self._cookies, self._state), stdin=subprocess.DEVNULL,
                stdout=fd, stderr=fd, cwd=self._downloads, process_group=0)
        except OSError as error:
            _close(fd)
            # The program or the working directory, as the OS names it.
            raise DownloadinuxError(_reason(error, error.filename or RUNNER)) from None
        except Exception:
            _close(fd)
            raise DownloadinuxError(LAUNCH_FAILED) from None
        self._running[record.id] = process
        _close(fd)

    def poll(self) -> tuple[job_store.JobRecord, ...]:
        """End every exited runner through the store, in job ID order, without blocking.

        A runner killed by signal N ends with 128 + N, as the runner maps
        its own child. Each end passes whether shutdown has begun, so §3's
        row order decides the state. A runner leaves the registry only after
        its end commits: a failed end propagates, and a later poll ends it
        again. Returns the ended records in order.
        """
        self._refuse_stopped()
        return self._poll()

    def _poll(self) -> tuple[job_store.JobRecord, ...]:
        ended = []
        for job_id in sorted(self._running):
            status = self._running[job_id].poll()
            if status is None:
                continue
            record = self._store.end(job_id, exit_status=128 - status if status < 0 else status,
                                     stopping=self._stopping)
            del self._running[job_id]
            ended.append(record)
        return tuple(ended)

    def cancel(self, job_ids) -> tuple[job_store.CancelOutcome, ...]:
        """Cancel through the store, then `SIGTERM` each runner it says to signal (§5 *Cancel signals*).

        After the store's commit, in outcome order, each SIGNAL outcome's
        registered runner process gets `SIGTERM`, never its group: the runner
        forwards it to the downloader's group (DEC-36). A runner known to
        have exited, or a job with no registered runner, gets none. Every
        signal is attempted; the first OS error other than a vanished
        process then raises `cannot signal the runner of job <id>: <reason>`.
        Otherwise returns the store's outcomes unchanged.
        """
        self._refuse_stopped()
        outcomes = self._store.cancel(job_ids)
        self._raise_first(self._signal(o.id for o in outcomes if o.outcome == job_store.SIGNAL))
        return outcomes

    def shutdown(self, scheduler) -> tuple[int, ...]:
        """Stop, signal, wait at most 20 seconds, then mark (§5 *Shutdown*); one blocking call.

        Stops the scheduler, so nothing more starts; from then on every end
        passes stopping. Sends `SIGTERM` to every registered runner in job
        ID order, as cancel does, then polls until none is registered or
        20 seconds have passed since those signals on the monotonic clock,
        sleeping at most 0.1 seconds between polls, never past that deadline,
        and polling once more at it. The store's shutdown mark then records
        every job still running as `interrupted` with `worker stopped`;
        returns the marked IDs. Runners still alive are left to the
        container's stop: no `SIGKILL`. A signal error is raised as cancel
        raises it, once the wait and the mark are done (DEC-49). Afterwards
        every launch, poll, cancel, and shutdown raises `the worker has
        stopped`.
        """
        self._refuse_stopped()
        scheduler.stop()
        self._stopping = True
        failure = self._signal(sorted(self._running))
        deadline = self._monotonic() + SHUTDOWN_WAIT
        while True:
            self._poll()
            if not self._running:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(POLL_INTERVAL, remaining))
        marked = self._store.mark_stopped()
        self._stopped = True
        self._raise_first(failure)
        return marked

    def _signal(self, job_ids) -> tuple[int, OSError] | None:
        """`SIGTERM` each job's registered runner process; return the first failure."""
        failure = None
        for job_id in job_ids:
            process = self._running.get(job_id)
            if process is None or process.returncode is not None:
                continue
            try:
                # Polls first, skipping a reaped runner, and ignores a
                # vanished PID (ProcessLookupError).
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                if failure is None:
                    failure = (job_id, error)
        return failure

    @staticmethod
    def _raise_first(failure: tuple[int, OSError] | None) -> None:
        if failure is not None:
            job_id, error = failure
            # A reasonless OS error reads `signal failed` (DEC-49).
            raise DownloadinuxError(
                f'cannot signal the runner of job {job_id}: {error.strerror or "signal failed"}') from None

    def _refuse_stopped(self) -> None:
        if self._stopped:
            raise DownloadinuxError(STOPPED)

    def _open_log(self, log: Path) -> int:
        try:
            directory = os.open(self._store.logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise DownloadinuxError(_reason(error, log)) from None
        fd = None
        try:
            fd = os.open(log.name, LOG_FLAGS, 0o600, dir_fd=directory)
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                raise DownloadinuxError(f'{log}: not a regular file')
            if stat.S_IMODE(status.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        except OSError as error:
            if fd is not None:
                _close(fd)
            raise DownloadinuxError(_reason(error, log)) from None
        except BaseException:
            if fd is not None:
                _close(fd)
            raise
        finally:
            _close(directory)
        return fd


def _close(fd: int) -> None:
    """Close a descriptor the worker no longer needs; a close error changes no outcome."""
    try:
        os.close(fd)
    except OSError:
        pass
