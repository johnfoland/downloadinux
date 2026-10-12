"""Worker-owned SQLite job store, standard library only (DEC-41, DEC-45, DEC-46).

Target design, Persistent worker and jobs §2 and §5, "Store lifecycle". This
library has no service, scheduler, process, cookie reads, or host
configuration dependencies; the scheduler and service decide only *when* to
call its lifecycle operations.
"""
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import random
import sqlite3
import stat
from typing import Callable

from . import cookie_store
from .errors import DownloadinuxError

DIRECTORY = 'jobs'
DATABASE = 'jobs.sqlite3'
LOCK = '.worker.lock'
LOGS = 'logs'
PRESETS = ('video', 'gallery')
STATES = ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')
RECORD_FIELDS = ('id', 'submission', 'preset', 'url', 'lane', 'source', 'revision',
                 'user_agent', 'state', 'wait', 'attempt', 'cancel_requested',
                 'created_at', 'queued_at', 'started_at', 'finished_at', 'exit_status', 'error')

SCHEMA = (
    '''CREATE TABLE submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL)''',
    '''CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        submission INTEGER NOT NULL REFERENCES submissions(id),
        preset TEXT NOT NULL CHECK (preset IN ('video', 'gallery')),
        url TEXT NOT NULL COLLATE BINARY, lane TEXT CHECK (lane IS NULL OR lane = preset),
        source TEXT, revision TEXT, user_agent TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
        wait TEXT, attempt INTEGER NOT NULL CHECK (attempt > 0),
        cancel_requested INTEGER NOT NULL CHECK (cancel_requested IN (0, 1)),
        created_at TEXT NOT NULL, queued_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, exit_status INTEGER, error TEXT,
        queue_order INTEGER NOT NULL CHECK (queue_order > 0),
        CHECK ((source IS NULL) = (revision IS NULL)))''',
    '''CREATE TABLE lanes (
        preset TEXT PRIMARY KEY NOT NULL CHECK (preset IN ('video', 'gallery')),
        next_start_at TEXT)''',
    '''CREATE TABLE queue_counter (
        id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL CHECK (value >= 0))''',
    '''CREATE UNIQUE INDEX active_jobs ON jobs (preset COLLATE BINARY, url COLLATE BINARY)
        WHERE state IN ('queued', 'running')''',
)
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
THROTTLE_SECONDS = (120, 289)  # §4: inclusive bounds of a lane's delay (DEC-6, DEC-43)
TABLE_COLUMNS = {
    'submissions': {'id': 'INTEGER', 'created_at': 'TEXT'},
    'jobs': {name: ('INTEGER' if name in ('id', 'submission', 'attempt', 'cancel_requested',
                                        'exit_status', 'queue_order') else 'TEXT')
             for name in (*RECORD_FIELDS, 'queue_order')},
    'lanes': {'preset': 'TEXT', 'next_start_at': 'TEXT'},
    'queue_counter': {'id': 'INTEGER', 'value': 'INTEGER'},
}


@dataclass(frozen=True)
class Busy:
    """Another worker owns this root; the service may wait and try again."""


BUSY = Busy()


@dataclass(frozen=True)
class JobReference:
    id: int
    url: str


@dataclass(frozen=True)
class SubmissionResult:
    submission: int
    created: tuple[JobReference, ...]
    duplicates: tuple[JobReference, ...]


@dataclass(frozen=True)
class JobRecord:
    """Independent public projection; queue order and deadlines stay internal."""
    id: int
    submission: int
    preset: str
    url: str
    lane: str | None
    source: str | None
    revision: str | None
    user_agent: str
    state: str
    wait: dict[str, str] | None
    attempt: int
    cancel_requested: bool
    created_at: str
    queued_at: str
    started_at: str | None
    finished_at: str | None
    exit_status: int | None
    error: str | None


@dataclass(frozen=True)
class QueueHead:
    """One queue holding a queued job (§5 "Store lifecycle", *Queues*).

    A lane queue has `lane` set to its preset and `submission` None; an
    immediate queue has `lane` None and its `submission`. `next_start_at` is
    the lane's stored deadline, always None for an immediate queue.
    """
    lane: str | None
    submission: int | None
    head: JobRecord
    running: bool
    next_start_at: str | None


CANCELLED, SIGNAL, REFUSED, UNKNOWN = 'cancelled', 'signal', 'refused', 'unknown'


@dataclass(frozen=True)
class CancelOutcome:
    """One ID's cancel outcome (§5 *Cancel*).

    `outcome` is CANCELLED (a queued job ended), SIGNAL (a running job's
    cancel was requested; the worker signals its runner after the commit),
    REFUSED (a finished job, `state` naming its state), or UNKNOWN. `state`
    is the job's state after the call, None when unknown.
    """
    id: int
    outcome: str
    state: str | None


REQUEUED, SHADOWED = 'requeued', 'shadowed'
RETRYABLE = ('failed', 'cancelled', 'interrupted')


@dataclass(frozen=True)
class RetryOutcome:
    """One ID's retry outcome (§3 *Retry*, §5 *Retry*).

    `outcome` is REQUEUED, REFUSED (a queued, running, or succeeded job,
    `state` naming its state), SHADOWED (`holder` is the queued or running
    job holding its preset and URL, DEC-46), or UNKNOWN. `state` is the
    job's state after the call, None when unknown.
    """
    id: int
    outcome: str
    state: str | None
    holder: int | None = None


@dataclass(frozen=True)
class RetryResult:
    """The new shared submission, None when nothing was requeued."""
    submission: int | None
    outcomes: tuple[RetryOutcome, ...]


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise DownloadinuxError('job or submission ID must be a positive integer')
    return value


def _valid_timestamp(value: object) -> bool:
    """Named cookie sources §1's UTC form, ASCII digits only (issue #69)."""
    if type(value) is not str or cookie_store.TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def _decode_record(row, path: Path) -> JobRecord:
    values = dict(zip(RECORD_FIELDS, row))
    values['cancel_requested'] = bool(values['cancel_requested'])
    if values['wait'] is not None:
        try:
            wait = json.loads(values['wait'])
            valid = type(wait) is dict and wait == {'reason': 'vpn'}
            if type(wait) is dict and set(wait) == {'reason', 'until'} and wait['reason'] == 'throttle':
                valid = _valid_timestamp(wait['until'])
            if not valid or values['state'] != 'queued':
                raise ValueError('invalid wait')
        except Exception:
            # Unreadable stored JSON includes decoder recursion failures.
            raise DownloadinuxError(f'{path}: invalid stored job wait') from None
        values['wait'] = wait
    return JobRecord(**values)


def _validate_selection(source, revision, user_agent) -> None:
    """Pinned selection and user-agent, shared by submission and retry (DEC-45, DEC-46)."""
    if source is not None or revision is not None:
        if (type(source) is not str or cookie_store.NAME.fullmatch(source) is None
                or type(revision) is not str or cookie_store.REVISION.fullmatch(revision) is None):
            raise DownloadinuxError('cookie selection requires both valid source and revision identifiers')
    if type(user_agent) is not str or any(char in user_agent for char in '\r\n\0'):
        raise DownloadinuxError('user-agent must be a string without CR, LF, or NUL')


def _validate_submission(preset, urls, lane, source, revision, user_agent) -> tuple[str, ...]:
    """Validate the entire input before IDs or queues can change (DEC-45)."""
    if type(preset) is not str or preset not in PRESETS:
        raise DownloadinuxError('submission preset must be video or gallery')
    if lane is not None and (type(lane) is not str or lane != preset):
        raise DownloadinuxError('submission lane must be null or its preset')
    if not isinstance(urls, Sequence) or isinstance(urls, (str, bytes, bytearray)) or not urls:
        raise DownloadinuxError('submission URLs must be a nonempty ordered sequence')
    urls = tuple(urls)
    for url in urls:
        if type(url) is not str or not url or any(char in url for char in '\r\n\0'):
            raise DownloadinuxError('submission URLs must be nonempty strings without CR, LF, or NUL')
    _validate_selection(source, revision, user_agent)
    return urls


def _job_ids(job_ids, operation: str) -> tuple[int, ...]:
    """A nonempty ordered sequence of positive IDs; repeats collapse to the first."""
    if not isinstance(job_ids, Sequence) or isinstance(job_ids, (str, bytes, bytearray)) or not job_ids:
        raise DownloadinuxError(f'{operation} requires a nonempty ordered sequence of job IDs')
    return tuple(dict.fromkeys(_positive_id(job_id) for job_id in job_ids))


def _retry_entries(entries) -> dict[int, tuple[str | None, str | None, str]]:
    """Validate every entry before anything changes; repeats collapse to the first."""
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)) or not entries:
        raise DownloadinuxError('retry requires a nonempty ordered sequence of entries')
    validated = {}
    for entry in entries:
        if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes, bytearray)) or len(entry) != 4:
            raise DownloadinuxError('retry entries must be (job ID, source, revision, user-agent)')
        job_id, source, revision, user_agent = entry
        _positive_id(job_id)
        _validate_selection(source, revision, user_agent)
        validated.setdefault(job_id, (source, revision, user_agent))
    return validated


def log_name(job_id: int, attempt: int) -> str:
    """An attempt's log file name, `<id>-<attempt>.log`, inside `JobStore.logs` (§2)."""
    if type(job_id) is not int or job_id <= 0 or type(attempt) is not int or attempt <= 0:
        raise DownloadinuxError('job ID and attempt must be positive integers')
    return f'{job_id}-{attempt}.log'


def _default_delay() -> int:
    return random.randint(*THROTTLE_SECONDS)


def _os_error(path: Path, error: OSError) -> DownloadinuxError:
    return DownloadinuxError(f'{path}: {error.strerror or "filesystem operation failed"}')


def _sqlite_error(path: Path, error: sqlite3.Error) -> DownloadinuxError:
    # Never expose SQLite's raw message: triggers can include submitted values.
    category = ('constraint failure' if isinstance(error, sqlite3.IntegrityError)
                else 'database operation failed')
    return DownloadinuxError(f'{path}: SQLite {category}')


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


class _NotOwned(Exception):
    """The managed file is not a provably identical regular file."""


def _create_regular(dir_fd: int, name: str, *, exclusive: bool = False) -> tuple[int, int]:
    """Create/find, close, then let _reopen supply the locking descriptor.

    Same two-open contract as cookie_snapshot (DEC-35); the bounded ENOENT
    retry handles macOS concurrent directory-relative creation.
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    if exclusive:
        flags |= os.O_EXCL
    for attempt in range(8):
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
            break
        except FileNotFoundError:
            if exclusive or attempt == 7:
                raise
    try:
        status = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(status.st_mode):
        raise _NotOwned(name)
    return _identity(status)


def _reopen(dir_fd: int, name: str, identity: tuple[int, int]) -> int:
    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or _identity(status) != identity:
            raise _NotOwned(name)
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _private_sidecars(dir_fd: int) -> None:
    # SQLite may recover a retained journal on open. Check before it can follow
    # a foreign symlink, and make any retained rollback journal private.
    for name in (DATABASE + '-journal', DATABASE + '-wal', DATABASE + '-shm'):
        try:
            status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(status.st_mode):
            raise _NotOwned(name)
        fd = _reopen(dir_fd, name, _identity(status))
        os.close(fd)


def _timestamp(clock: Callable[[], datetime]) -> str:
    instant = clock()
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise DownloadinuxError('job-store clock must return a timezone-aware time')
    return instant.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


class JobStore:
    """An open connection whose lifetime is enclosed by worker ownership."""

    def __init__(self, path: Path, connection: sqlite3.Connection, lock_fd: int,
                 clock: Callable[[], datetime], delay: Callable[[], int] = _default_delay):
        self.path = path
        self._connection = connection
        self._lock_fd = lock_fd
        self._clock = clock
        self._delay = delay
        self._closed = False

    @property
    def logs(self) -> Path:
        """The owned store's attempt-log directory, `jobs/logs` beside the database (§2)."""
        return self.path.parent / LOGS

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise _sqlite_error(self.path, error) from None
        finally:
            try:
                os.close(self._lock_fd)
            except OSError as error:
                raise _os_error(self.path.parent / LOCK, error) from None

    def submit(self, preset: str, urls: Sequence[str], *, lane: str | None = None,
               source: str | None = None, revision: str | None = None,
               user_agent: str = '') -> SubmissionResult:
        """Commit new work or references to exact active duplicates as one unit.

        This accepts pinned identifiers; host pinning and runner material
        checks stay at their own boundaries (DEC-31, DEC-45).
        """
        urls = _validate_submission(preset, urls, lane, source, revision, user_agent)
        timestamp = _timestamp(self._clock)
        created, duplicates = [], []
        with self._transaction() as c:
            submission = c.execute('INSERT INTO submissions(created_at) VALUES (?)', (timestamp,)).lastrowid
            for url in dict.fromkeys(urls):
                existing = c.execute('''SELECT id FROM jobs WHERE preset=? AND url=?
                                        AND state IN ('queued', 'running')''', (preset, url)).fetchone()
                if existing is not None:
                    duplicates.append(JobReference(existing[0], url))
                    continue
                c.execute('UPDATE queue_counter SET value=value+1 WHERE id=1')
                order = c.execute('SELECT value FROM queue_counter WHERE id=1').fetchone()[0]
                job = c.execute('''INSERT INTO jobs
                    (submission, preset, url, lane, source, revision, user_agent, state,
                     attempt, cancel_requested, created_at, queued_at, queue_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 1, 0, ?, ?, ?)''',
                    (submission, preset, url, lane, source, revision, user_agent, timestamp, timestamp, order)).lastrowid
                created.append(JobReference(job, url))
        return SubmissionResult(submission, tuple(created), tuple(duplicates))

    def _read(self, where: str, parameters: tuple) -> list[JobRecord]:
        try:
            rows = self._connection.execute(
                'SELECT ' + ', '.join(RECORD_FIELDS) + ' FROM jobs' + where + ' ORDER BY id', parameters)
            return [_decode_record(row, self.path) for row in rows]
        except sqlite3.Error as error:
            raise _sqlite_error(self.path, error) from None
        except (UnicodeError, OverflowError):
            raise DownloadinuxError(f'{self.path}: SQLite value encoding failed') from None

    def get(self, job_id: int) -> JobRecord:
        """Return one record, or refuse an invalid/unknown positive ID (§2)."""
        job_id = _positive_id(job_id)
        records = self._read(' WHERE id=?', (job_id,))
        if not records:
            raise DownloadinuxError(f'{self.path}: unknown job ID {job_id}')
        return records[0]

    def list(self, *, state: str | None = None, preset: str | None = None,
             submission: int | None = None) -> list[JobRecord]:
        """ID-ordered records; every caller value, including filters, is bound."""
        clauses, parameters = [], []
        for field, value in (('state', state), ('preset', preset), ('submission', submission)):
            if value is None:
                continue
            if field == 'submission':
                _positive_id(value)
            elif type(value) is not str:
                raise DownloadinuxError(f'{field} filter must be a string')
            clauses.append(field + '=?')
            parameters.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._read(where, tuple(parameters))

    def queue_heads(self) -> tuple[QueueHead, ...]:
        """Every queue holding a queued job, with its head (§5 *Queues*, DEC-46).

        The head is the queued job with the least queue order. Entries list
        the lanes by preset name (`gallery`, then `video`), then immediate
        queues by submission ID; queues with only running or finished jobs
        are absent.
        """
        heads = []
        with self._transaction() as c:
            for preset in sorted(PRESETS):
                entry = self._queue_head(c, 'lane=?', (preset,))
                if entry is not None:
                    heads.append(QueueHead(preset, None, *entry, self._deadline(c, preset)))
            submissions = [row[0] for row in c.execute(
                "SELECT DISTINCT submission FROM jobs WHERE lane IS NULL AND state='queued' ORDER BY submission")]
            for submission in submissions:
                head, running = self._queue_head(c, 'lane IS NULL AND submission=?', (submission,))
                heads.append(QueueHead(None, submission, head, running, None))
        return tuple(heads)

    def start(self, job_id: int) -> JobRecord:
        """Move a queued job to `running` (§5 *Start*, DEC-46); return its record.

        Raises `DownloadinuxError`, changing nothing, unless the job is
        queued, heads its queue, its queue has no running job, and, for a
        lane job, the lane's deadline is null or not after now. Unknown or
        invalid IDs are refused as `get` refuses them. The VPN gate is the
        scheduler's.
        """
        job_id = _positive_id(job_id)
        now = _timestamp(self._clock)
        with self._transaction() as c:
            job = self._job(c, job_id)
            if job.state != 'queued':
                raise DownloadinuxError(f'job {job_id} is {job.state}; only a queued job can start')
            where, parameters = self._queue_of(job)
            head, _ = self._queue_head(c, where, parameters)
            if head.id != job_id:
                raise DownloadinuxError(f'job {job_id} does not head its queue; job {head.id} does')
            running = c.execute(f"SELECT id FROM jobs WHERE {where} AND state='running' ORDER BY id",
                                parameters).fetchone()
            if running is not None:
                raise DownloadinuxError(f'job {job_id} cannot start while job {running[0]} runs in its queue')
            if job.lane is not None:
                deadline = self._deadline(c, job.lane)
                if deadline is not None and deadline > now:
                    raise DownloadinuxError(f'job {job_id} cannot start before its lane deadline {deadline}')
            c.execute("UPDATE jobs SET state='running', started_at=?, wait=NULL WHERE id=?", (now, job_id))
            return self._job(c, job_id)

    def set_wait(self, job_id: int, reason: str | None) -> JobRecord:
        """Set (`'vpn'`, `'throttle'`) or clear (None) a queued job's wait (§5 *Waits*).

        A throttle wait copies its `until` from the lane's stored deadline.
        Raises `DownloadinuxError`, changing nothing, for a job that is not
        queued, and for a throttle wait on an immediate job or a lane with
        no deadline. Returns the updated record.
        """
        job_id = _positive_id(job_id)
        if reason is not None and (type(reason) is not str or reason not in ('vpn', 'throttle')):
            raise DownloadinuxError('job wait reason must be vpn, throttle, or null')
        with self._transaction() as c:
            job = self._job(c, job_id)
            if job.state != 'queued':
                raise DownloadinuxError(f'job {job_id} is {job.state}; only a queued job can wait')
            wait = None
            if reason == 'vpn':
                wait = json.dumps({'reason': 'vpn'})
            elif reason == 'throttle':
                if job.lane is None:
                    raise DownloadinuxError(f'job {job_id} is immediate; only a lane job waits for the throttle')
                deadline = self._deadline(c, job.lane)
                if deadline is None:
                    raise DownloadinuxError(f'job {job_id} cannot wait for the throttle: its lane has no deadline')
                wait = json.dumps({'reason': 'throttle', 'until': deadline})
            c.execute('UPDATE jobs SET wait=? WHERE id=?', (wait, job_id))
            return self._job(c, job_id)

    def end(self, job_id: int, *, exit_status: int | None = None, launch_failure: str | None = None,
            stopping: bool = False) -> JobRecord:
        """End a running job (§5 *End*, §3's rows in order); return its record.

        Takes the runner's exit status (0 to 255) or a launch-failure reason,
        never both, and whether the worker is stopping. A lane job draws its
        lane's deadline in the same transaction (§5 *Lane deadline*). Raises
        `DownloadinuxError`, changing nothing, unless the job is running.
        """
        job_id = _positive_id(job_id)
        if (exit_status is None) == (launch_failure is None):
            raise DownloadinuxError('ending a job takes either an exit status or a launch-failure reason')
        if exit_status is not None and (type(exit_status) is not int or not 0 <= exit_status <= 255):
            raise DownloadinuxError('job exit status must be an integer from 0 to 255')
        if launch_failure is not None and (type(launch_failure) is not str or not launch_failure
                                           or '\0' in launch_failure):
            raise DownloadinuxError('job launch-failure reason must be a nonempty string without NUL')
        if type(stopping) is not bool:
            raise DownloadinuxError('job end stopping flag must be a boolean')
        now = _timestamp(self._clock)
        with self._transaction() as c:
            job = self._job(c, job_id)
            if job.state != 'running':
                raise DownloadinuxError(f'job {job_id} is {job.state}; only a running job can end')
            # The first matching row of §3 applies.
            if launch_failure is not None:
                state, error = 'failed', launch_failure
            elif exit_status == 0:
                state, error = 'succeeded', None
            elif job.cancel_requested:
                state, error = 'cancelled', None
            elif stopping:
                state, error = 'interrupted', 'worker stopped'
            else:
                state, error = 'failed', None
            c.execute('UPDATE jobs SET state=?, finished_at=?, exit_status=?, error=? WHERE id=?',
                      (state, now, exit_status, error, job_id))
            if job.lane is not None:
                self._draw_deadline(c, job.lane, now)
            return self._job(c, job_id)

    def cancel(self, job_ids: Sequence[int]) -> tuple[CancelOutcome, ...]:
        """Cancel jobs in one transaction, one outcome per ID in order (§5 *Cancel*).

        Repeats collapse to the first. A queued job becomes `cancelled` and
        draws no lane delay (DEC-46); a running job gets `cancel_requested`
        (again, if already set) and a SIGNAL outcome. Refused and unknown IDs
        are returned, not raised, and change nothing.
        """
        job_ids = _job_ids(job_ids, 'cancel')
        now = _timestamp(self._clock)
        outcomes = []
        with self._transaction() as c:
            for job_id in job_ids:
                row = c.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    outcomes.append(CancelOutcome(job_id, UNKNOWN, None))
                elif row[0] == 'queued':
                    c.execute("UPDATE jobs SET state='cancelled', cancel_requested=1, finished_at=?, wait=NULL"
                              ' WHERE id=?', (now, job_id))
                    outcomes.append(CancelOutcome(job_id, CANCELLED, 'cancelled'))
                elif row[0] == 'running':
                    c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
                    outcomes.append(CancelOutcome(job_id, SIGNAL, 'running'))
                else:
                    outcomes.append(CancelOutcome(job_id, REFUSED, row[0]))
        return tuple(outcomes)

    def retry(self, entries: Sequence[tuple[int, str | None, str | None, str]]) -> RetryResult:
        """Requeue finished jobs as one new submission (§3 *Retry*, §5 *Retry*, DEC-46).

        Each entry is (job ID, source, revision, user-agent): the selection
        the host pinned, validated as for a submission. Repeats collapse to
        the first. In request order, a failed, cancelled, or interrupted job
        whose preset and URL no queued or running job holds is requeued at
        the back of its queue; its lane is unchanged, so immediate jobs
        retried together form one queue keyed by the new submission, which
        is allocated only when something is requeued. Refused, shadowed, and
        unknown IDs are returned, not raised, and change nothing.
        """
        entries = _retry_entries(entries)
        now = _timestamp(self._clock)
        submission = None
        outcomes = []
        with self._transaction() as c:
            for job_id, (source, revision, user_agent) in entries.items():
                row = c.execute('SELECT state, preset, url FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    outcomes.append(RetryOutcome(job_id, UNKNOWN, None))
                    continue
                state, preset, url = row
                if state not in RETRYABLE:
                    outcomes.append(RetryOutcome(job_id, REFUSED, state))
                    continue
                # Look before updating, with the active index's exact binary
                # comparison, so the index never fires on a valid request and
                # a job this retry requeued already shadows later IDs.
                holder = c.execute('''SELECT id FROM jobs WHERE preset=? AND url=?
                                      AND state IN ('queued', 'running') ORDER BY id''', (preset, url)).fetchone()
                if holder is not None:
                    outcomes.append(RetryOutcome(job_id, SHADOWED, state, holder[0]))
                    continue
                if submission is None:
                    submission = c.execute('INSERT INTO submissions(created_at) VALUES (?)', (now,)).lastrowid
                c.execute('UPDATE queue_counter SET value=value+1 WHERE id=1')
                order = c.execute('SELECT value FROM queue_counter WHERE id=1').fetchone()[0]
                c.execute('''UPDATE jobs SET state='queued', submission=?, attempt=attempt+1, queue_order=?,
                             queued_at=?, source=?, revision=?, user_agent=?, cancel_requested=0, wait=NULL,
                             started_at=NULL, finished_at=NULL, exit_status=NULL, error=NULL WHERE id=?''',
                          (submission, order, now, source, revision, user_agent, job_id))
                outcomes.append(RetryOutcome(job_id, REQUEUED, 'queued'))
        return RetryResult(submission, tuple(outcomes))

    def reconcile(self) -> tuple[int, ...]:
        """Worker start: mark running jobs `interrupted` with `worker restarted`.

        Also clears every queued job's wait (§5 *Interrupted marks*, DEC-46).
        Returns the marked IDs in ID order. `open_store` never calls this.
        """
        return self._mark_interrupted('worker restarted', clear_waits=True)

    def mark_stopped(self) -> tuple[int, ...]:
        """Shutdown's final mark: running jobs become `interrupted` with `worker stopped`.

        Queued waits are untouched. Returns the marked IDs in ID order.
        """
        return self._mark_interrupted('worker stopped', clear_waits=False)

    def _mark_interrupted(self, error: str, *, clear_waits: bool) -> tuple[int, ...]:
        now = _timestamp(self._clock)
        with self._transaction() as c:
            running = c.execute("SELECT id, lane FROM jobs WHERE state='running' ORDER BY id").fetchall()
            for job_id, lane in running:
                c.execute("UPDATE jobs SET state='interrupted', finished_at=?, exit_status=NULL, error=?"
                          ' WHERE id=?', (now, error, job_id))
                if lane is not None:
                    self._draw_deadline(c, lane, now)
            if clear_waits:
                c.execute("UPDATE jobs SET wait=NULL WHERE state='queued'")
        return tuple(job_id for job_id, _ in running)

    def _draw_deadline(self, c: sqlite3.Connection, lane: str, finished_at: str) -> None:
        """Inside the ending transaction, so a crash cannot skip a throttle (DEC-46)."""
        delay = self._delay()
        if type(delay) is not int or delay < 0:
            raise DownloadinuxError('job-store delay source must return a nonnegative integer')
        try:
            deadline = (datetime.strptime(finished_at, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
                        + timedelta(seconds=delay)).strftime(TIMESTAMP_FORMAT)
        except OverflowError:
            raise DownloadinuxError('job-store delay is out of range') from None
        c.execute('UPDATE lanes SET next_start_at=? WHERE preset=?', (deadline, lane))

    def _job(self, c: sqlite3.Connection, job_id: int) -> JobRecord:
        row = c.execute('SELECT ' + ', '.join(RECORD_FIELDS) + ' FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise DownloadinuxError(f'{self.path}: unknown job ID {job_id}')
        return _decode_record(row, self.path)

    @staticmethod
    def _queue_of(job: JobRecord) -> tuple[str, tuple]:
        """A lane job's queue is its lane; an immediate job's is its submission."""
        if job.lane is not None:
            return 'lane=?', (job.lane,)
        return 'lane IS NULL AND submission=?', (job.submission,)

    def _queue_head(self, c: sqlite3.Connection, where: str, parameters: tuple
                    ) -> tuple[JobRecord, bool] | None:
        row = c.execute('SELECT ' + ', '.join(RECORD_FIELDS) + f" FROM jobs WHERE {where} AND state='queued'"
                        ' ORDER BY queue_order LIMIT 1', parameters).fetchone()
        if row is None:
            return None
        running = c.execute(f"SELECT EXISTS (SELECT 1 FROM jobs WHERE {where} AND state='running')",
                            parameters).fetchone()[0]
        return _decode_record(row, self.path), bool(running)

    def _deadline(self, c: sqlite3.Connection, preset: str) -> str | None:
        deadline = c.execute('SELECT next_start_at FROM lanes WHERE preset=?', (preset,)).fetchone()[0]
        if deadline is not None and not _valid_timestamp(deadline):
            raise DownloadinuxError(f'{self.path}: invalid stored lane deadline')
        return deadline

    @contextmanager
    def _transaction(self):
        """Commit is publication. Rollback is attempted even after commit fails."""
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            yield self._connection
            self._connection.commit()
        except BaseException as error:
            try:
                self._connection.rollback()
            except sqlite3.Error as rollback_error:
                raise _sqlite_error(self.path, rollback_error) from None
            if isinstance(error, sqlite3.Error):
                raise _sqlite_error(self.path, error) from None
            if isinstance(error, UnicodeError):
                raise DownloadinuxError(f'{self.path}: SQLite text encoding failed') from None
            if isinstance(error, OverflowError):
                raise DownloadinuxError(f'{self.path}: SQLite integer out of range') from None
            raise

    def _initialize(self) -> None:
        c = self._connection
        version = c.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not r[0].startswith('sqlite_')}
        if version == 0 and not tables:
            with self._transaction():
                for statement in SCHEMA:
                    c.execute(statement)
                c.executemany('INSERT INTO lanes (preset) VALUES (?)', [(p,) for p in PRESETS])
                c.execute('INSERT INTO queue_counter VALUES (1, 0)')
                c.execute('PRAGMA user_version=1')
        elif version != 1:
            raise DownloadinuxError(f'{self.path}: unsupported or nonempty unversioned job store')
        self._check_structure()

    def _check_structure(self) -> None:
        c = self._connection
        for table, expected in TABLE_COLUMNS.items():
            columns = {r[1]: r[2].upper() for r in c.execute(f'PRAGMA table_info({table})')}
            if not expected.items() <= columns.items():
                raise DownloadinuxError(f'{self.path}: missing required job-store structure')
        # Definitions are ours in schema v1: verify constraints, collation,
        # foreign key, and AUTOINCREMENT as well as column names/types.
        for statement in SCHEMA:
            name = statement.split()[3] if statement.startswith('CREATE UNIQUE') else statement.split()[2]
            row = c.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if row is None or ' '.join(row[0].split()) != ' '.join(statement.split()):
                raise DownloadinuxError(f'{self.path}: invalid required job-store structure')
        if ({r[0] for r in c.execute('SELECT preset FROM lanes')} != set(PRESETS)
                or c.execute('SELECT id, value FROM queue_counter').fetchall() == []):
            raise DownloadinuxError(f'{self.path}: invalid lanes or queue counter')
        counter = c.execute('SELECT id, value FROM queue_counter').fetchall()
        if len(counter) != 1 or counter[0][0] != 1 or type(counter[0][1]) is not int or counter[0][1] < 0:
            raise DownloadinuxError(f'{self.path}: invalid queue counter')
        if (c.execute('PRAGMA quick_check').fetchall() != [('ok',)]
                or c.execute('PRAGMA foreign_key_check').fetchone() is not None):
            raise DownloadinuxError(f'{self.path}: damaged job store')


def open_store(state: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
               delay: Callable[[], int] = _default_delay) -> JobStore | Busy:
    """Try nonblocking ownership, then open/initialize. Never create state.

    `delay` draws a lane's throttle delay in whole seconds (§4, §5 *Lane
    deadline*); tests inject it.
    """
    state = Path(state)
    path = state / DIRECTORY
    lock_fd = None
    connection = None
    root_fd = jobs_fd = None
    current_path = state
    try:
        # Configured root symlinks are allowed; managed children are not.
        root_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        current_path = path
        jobs_fd = _directory(root_fd, DIRECTORY)
        current_path = path / LOCK
        identity = _create_regular(jobs_fd, LOCK)
        lock_fd = _reopen(jobs_fd, LOCK, identity)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return BUSY
            raise
        current_path = path / LOGS
        logs_fd = _directory(jobs_fd, LOGS)
        os.close(logs_fd)
        current_path = path / DATABASE
        identity = _create_regular(jobs_fd, DATABASE)
        db_fd = _reopen(jobs_fd, DATABASE, identity)
        os.close(db_fd)
        _private_sidecars(jobs_fd)
        connection = sqlite3.connect(current_path, isolation_level=None)
        if _identity(os.stat(DATABASE, dir_fd=jobs_fd, follow_symlinks=False)) != identity:
            raise _NotOwned(DATABASE)
        if connection.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
            raise DownloadinuxError(f'{current_path}: SQLite rollback journal unavailable')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA foreign_keys=ON')
        if (connection.execute('PRAGMA synchronous').fetchone() != (2,)
                or connection.execute('PRAGMA foreign_keys').fetchone() != (1,)):
            raise DownloadinuxError(f'{current_path}: SQLite durability settings unavailable')
        store = JobStore(current_path, connection, lock_fd, clock, delay)
        store._initialize()
        # Close directory handles before transferring the connection/owner, so
        # a failed directory close still takes the failed-open cleanup path.
        os.close(jobs_fd)
        jobs_fd = None
        os.close(root_fd)
        root_fd = None
        connection = lock_fd = None  # ownership transferred to the store
        return store
    except OSError as error:
        raise _os_error(current_path, error) from None
    except _NotOwned as error:
        raise DownloadinuxError(f'{path / str(error)}: invalid managed regular file identity') from None
    except sqlite3.Error as error:
        raise _sqlite_error(path / DATABASE, error) from None
    finally:
        # Attempt all closure, connection first; never delete unproven residue.
        cleanup_error = None
        try:
            if connection is not None:
                connection.close()
        except sqlite3.Error as error:
            cleanup_error = _sqlite_error(path / DATABASE, error)
        finally:
            for fd in (lock_fd, jobs_fd, root_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError as error:
                        cleanup_error = _os_error(current_path, error)
        if cleanup_error is not None:
            raise cleanup_error from None
