"""Worker-owned SQLite foundation, standard library only (DEC-41, DEC-45).

Target design, Persistent worker and jobs §2. This library has no service,
state transitions, cookie reads, or host configuration dependencies.
"""
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Callable

from .errors import DownloadinuxError

DIRECTORY = 'jobs'
DATABASE = 'jobs.sqlite3'
LOCK = '.worker.lock'
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
    if source is not None or revision is not None:
        if (type(source) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', source) is None
                or type(revision) is not str or re.fullmatch(r'[a-f0-9]{32}', revision) is None):
            raise DownloadinuxError('submission requires both valid source and revision identifiers')
    if type(user_agent) is not str or any(char in user_agent for char in '\r\n\0'):
        raise DownloadinuxError('submission user-agent must be a string without CR, LF, or NUL')
    return urls


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
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class JobStore:
    """An open connection whose lifetime is enclosed by worker ownership."""

    def __init__(self, path: Path, connection: sqlite3.Connection, lock_fd: int,
                 clock: Callable[[], datetime]):
        self.path = path
        self._connection = connection
        self._lock_fd = lock_fd
        self._clock = clock
        self._closed = False

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


def open_store(state: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
               ) -> JobStore | Busy:
    """Try nonblocking ownership, then open/initialize. Never create state."""
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
        current_path = path / 'logs'
        logs_fd = _directory(jobs_fd, 'logs')
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
        store = JobStore(current_path, connection, lock_fd, clock)
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
