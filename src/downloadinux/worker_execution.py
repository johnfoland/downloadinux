"""The worker's runner execution: launch, attempt logs, and exit polls (DEC-48).

Target design, *Persistent worker and jobs* §4 *Execution* and §5, "Runner
execution, cancel signals, and the shutdown wait". Standard library only (§1,
DEC-41): no settings, cookie material, or host paths. It uses the owned job
store's public operations and gives the scheduler its launch callback
(DEC-47); the service decides when to poll. Calls are serial on the store's
owning thread.
"""
import os
from pathlib import Path
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
        self._stopping = False

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
