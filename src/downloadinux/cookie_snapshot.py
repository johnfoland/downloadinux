"""Private invocation snapshots: allocation, teardown, cleanup (DEC-31, DEC-34).

Container-side and standard-library only (Named cookie sources §3, "Snapshot
storage format and interrupted allocation"). Roots are explicit: nothing here
loads host settings, reads the registry or a default, migrates, or touches a
browser. The configured state root may be a symlink; ``cookie-invocations``
and every entry below it are opened without following symlinks.

Snapshots hold credential material: errors name paths and reasons, never
cookie contents.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Callable

from . import cookie_store as store
from .errors import DownloadinuxError

DIRECTORY = 'cookie-invocations'
LOCK = '.lock'
STAGING = '.staging-'
JAR = 'cookies.txt'
METADATA = 'metadata.json'
LEASE = 'lease'
FILES = (JAR, METADATA, LEASE)
FIELDS = ('version', 'invocation_id', 'source', 'revision', 'created_at', 'user_agent')
ATTEMPTS = 8
METADATA_LIMIT = 64 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _failure(path: Path, error: OSError, suffix: str = '') -> DownloadinuxError:
    return DownloadinuxError(f'{path}: {error.strerror or "filesystem operation failed"}{suffix}')


def _invalid(path: Path, what: str) -> DownloadinuxError:
    return DownloadinuxError(f'{path}: invalid {what}; snapshots are managed by downloadinux')


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def validate_user_agent(value: object) -> str:
    """Empty is allowed (§4); CR/LF would split a header (DEC-34)."""
    if type(value) is not str or '\r' in value or '\n' in value:
        raise DownloadinuxError('user-agent must be a single line; set downloads.user_agent or --user-agent without CR/LF')
    return value


def metadata_record(invocation_id: str, pinned: store.Pinned, created_at: str, user_agent: str) -> dict:
    return dict(version=1, invocation_id=invocation_id, source=pinned.source,
                revision=pinned.revision, created_at=created_at, user_agent=user_agent)


def validate_metadata(record: object, path: Path, invocation_id: str) -> dict:
    """Exact version-1 metadata (DEC-34); diagnostic context, never authority."""
    if type(record) is not dict or set(record) != set(FIELDS):
        raise _invalid(path, 'snapshot metadata fields')
    if type(record['version']) is not int or record['version'] != 1:
        raise _invalid(path, 'snapshot metadata version')
    if (type(record['invocation_id']) is not str or not store.REVISION.fullmatch(record['invocation_id'])
            or record['invocation_id'] != invocation_id):
        raise _invalid(path, 'invocation ID')
    if type(record['source']) is not str or not store.NAME.fullmatch(record['source']):
        raise _invalid(path, 'source name')
    if type(record['revision']) is not str or not store.REVISION.fullmatch(record['revision']):
        raise _invalid(path, 'revision')
    if not store.valid_timestamp(record['created_at']):
        raise _invalid(path, 'creation timestamp')
    try:
        validate_user_agent(record['user_agent'])
    except DownloadinuxError:
        raise _invalid(path, 'user-agent') from None
    return record


def parse_metadata(data: bytes, path: Path, invocation_id: str) -> dict:
    try:
        record = json.loads(data.decode('utf-8'), object_pairs_hook=store.unique_object)
    except Exception:
        # Any decoder failure means unreadable input, not a code fault.
        raise _invalid(path, 'snapshot metadata JSON') from None
    return validate_metadata(record, path, invocation_id)


def _timestamp(clock: Callable[[], datetime]) -> str:
    instant = clock()
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise DownloadinuxError('snapshot clock must return a timezone-aware time')
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _open_root(directory: Path, *, create: bool) -> int | None:
    """The managed invocation root as a no-follow descriptor (§3)."""
    try:
        if create:
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
        try:
            fd = os.open(directory, _DIRECTORY_FLAGS)
        except FileNotFoundError:
            if create:
                raise
            return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _invalid(directory, 'managed directory') from None
        raise _failure(directory, error) from None
    try:
        if create:
            os.fchmod(fd, 0o700)
    except OSError as error:
        os.close(fd)
        raise _failure(directory, error) from None
    return fd


class _NotOwned(Exception):
    """A reopened entry is not the regular file just created or found."""


def _create_regular(dir_fd: int, name: str, *, exclusive: bool) -> tuple[int, int]:
    """Create (or find) a regular file and return its identity."""
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_EXCL if exclusive else 0)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        status = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(status.st_mode):
        raise _NotOwned(name)
    return _identity(status)


def _reopen(dir_fd: int, name: str, identity: tuple[int, int]) -> int:
    """Open the existing file again for locking (DEC-35).

    Docker Desktop's macOS file sharing ignores a lock taken through the
    descriptor that created the file: other opens can still acquire it.
    Locks on a descriptor that opened the existing file are enforced.
    """
    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if _identity(os.fstat(fd)) != identity:
            raise _NotOwned(name)
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


class _AllocationLock:
    """The persistent ``.lock`` shared by every producer and cleanup (DEC-34)."""

    def __init__(self, root_fd: int, directory: Path):
        self.root_fd, self.path = root_fd, directory / LOCK

    def __enter__(self):
        try:
            identity = _create_regular(self.root_fd, LOCK, exclusive=False)
            self.fd = _reopen(self.root_fd, LOCK, identity)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise _invalid(self.path, 'allocation lock') from None
            raise _failure(self.path, error) from None
        except _NotOwned:
            raise _invalid(self.path, 'allocation lock') from None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException as error:
            os.close(self.fd)
            if isinstance(error, OSError):
                raise _failure(self.path, error) from None
            raise
        return self

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
        except OSError as error:
            if exc[0] is None:
                raise _failure(self.path, error) from None


def _exists(root_fd: int, name: str, path: Path) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _failure(path, error) from None
    return True


@dataclass(eq=False, repr=False)
class Invocation:
    """An owned, published snapshot holding its lease until ``close`` (§3).

    ``lease_fd`` stays open and exclusively locked. Like any descriptor
    Python opens it is not inherited by default; a runner passes it to the
    processes that use the jar (for example ``pass_fds``), so the lease stays
    held while any of them lives.
    """
    invocation_id: str
    path: Path
    metadata: dict
    lease_fd: int
    _root_fd: int
    _dir_fd: int
    _identity: tuple[int, int]
    _lease_identity: tuple[int, int]
    _name: str
    _closed: bool = field(default=False)

    @property
    def jar(self) -> Path:
        """The only cookie path a downloader receives (DEC-20)."""
        return self.path / JAR

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        """Owner teardown: remove the owned directory, then release the lease.

        The caller must have finished every consumer. Ownership is verified
        before each removal; anything unprovable or undeletable is left and
        reported (DEC-27). Descriptors are closed even when removal fails.
        """
        if self._closed:
            return
        self._closed = True
        try:
            _remove_owned(self._root_fd, self.path.parent, self._name, self._dir_fd,
                          self._identity, self._lease_identity)
        finally:
            _close_all(self.path, (self.lease_fd, self._dir_fd, self._root_fd))


def _close_all(path: Path, fds) -> None:
    failure = None
    for fd in fds:
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError as error:
            failure = failure or error
    if failure is not None:
        raise _failure(path, failure)


def _remove_owned(root_fd: int, directory: Path, name: str, dir_fd: int | None,
                  identity: tuple[int, int] | None, lease_identity: tuple[int, int] | None) -> None:
    """Remove known regular files and the directory, verified as ours (DEC-27, DEC-34).

    The directory must still be the one created; its lease must still be the
    locked file. No recursive traversal: unknown children make removal fail
    and stay as reported residue.
    """
    path = directory / name
    residue = '; snapshot residue may remain'
    if dir_fd is None or identity is None:
        raise DownloadinuxError(f'{path}: snapshot ownership cannot be verified{residue}')

    def still_owned() -> None:
        try:
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as error:
            raise _failure(path, error, residue) from None
        if _identity(current) != identity:
            raise DownloadinuxError(f'{path}: replaced by another entry; not removed')

    still_owned()
    present = []
    for child in FILES:
        target = path / child
        try:
            status = os.stat(child, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _failure(target, error, residue) from None
        if not stat.S_ISREG(status.st_mode) or (child == LEASE and _identity(status) != lease_identity):
            raise DownloadinuxError(f'{target}: not the owned snapshot file; not removed{residue}')
        present.append(child)
    # The lease file goes last, so a partial removal still shows its lease.
    for child in present:
        try:
            os.unlink(child, dir_fd=dir_fd)
        except OSError as error:
            raise _failure(path / child, error, residue) from None
    still_owned()
    try:
        os.rmdir(name, dir_fd=root_fd)
    except OSError as error:
        raise _failure(path, error, residue) from None


def _write(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _reserve(root_fd: int, directory: Path, id_factory: Callable[[], str]) -> str:
    """Pick an unused ID and create its staging directory; never clobber."""
    for _ in range(ATTEMPTS):
        invocation_id = id_factory()
        if type(invocation_id) is not str or not store.REVISION.fullmatch(invocation_id):
            raise _invalid(directory, 'invocation ID')
        staging = STAGING + invocation_id
        if _exists(root_fd, invocation_id, directory / invocation_id) or _exists(root_fd, staging, directory / staging):
            continue
        try:
            os.mkdir(staging, 0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        except OSError as error:
            raise _failure(directory / invocation_id, error) from None
        return invocation_id
    raise DownloadinuxError(f'{directory}: no unused invocation ID after {ATTEMPTS} attempts; retry')


def _publish(root_fd: int, directory: Path, data: bytes, pinned: store.Pinned,
             created_at: str, user_agent: str, id_factory: Callable[[], str]) -> Invocation:
    """Caller holds the allocation lock (DEC-34): stage, lease, sync, rename."""
    invocation_id = _reserve(root_fd, directory, id_factory)
    path = directory / invocation_id
    name = STAGING + invocation_id
    dir_fd = identity = lease_fd = lease_identity = None
    try:
        dir_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        identity = _identity(os.fstat(dir_fd))
        os.fchmod(dir_fd, 0o700)
        lease_identity = _create_regular(dir_fd, LEASE, exclusive=True)
        lease_fd = _reopen(dir_fd, LEASE, lease_identity)
        # The lease is held before the snapshot can become visible (§3).
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        metadata = metadata_record(invocation_id, pinned, created_at, user_agent)
        _write(dir_fd, JAR, data)
        _write(dir_fd, METADATA, (json.dumps(metadata) + '\n').encode())
        os.fsync(dir_fd)
        if _exists(root_fd, invocation_id, path):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
        os.rename(name, invocation_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        name = invocation_id
        os.fsync(root_fd)
    except BaseException as error:
        if isinstance(error, _NotOwned):
            error = DownloadinuxError(f'{path}: lease replaced during allocation')
        # Failure cleanup reports residue it cannot prove or remove; that
        # report takes precedence over the original failure (DEC-27).
        try:
            _remove_owned(root_fd, directory, name, dir_fd, identity, lease_identity)
        finally:
            _close_all(path, (lease_fd, dir_fd))
        if isinstance(error, OSError):
            raise _failure(path, error) from None
        if isinstance(error, DownloadinuxError):
            raise error from None
        raise
    return Invocation(invocation_id, path, metadata, lease_fd, root_fd, dir_fd, identity, lease_identity, name)


def allocate(cookie_root: Path, state_root: Path, pinned: store.Pinned | store.Anonymous,
             user_agent: str, *, id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
             clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Invocation | None:
    """Publish a private copy of the pinned revision's exact bytes (§3, DEC-34).

    Anonymous returns None: no jar, metadata, lease, or invocation root. A
    named pin reads only its retained revision; nothing resolves a default.
    """
    validate_user_agent(user_agent)
    if type(pinned) is store.Anonymous:
        return None
    if type(pinned) is not store.Pinned:
        raise store.invalid(cookie_root, 'pinned source')
    revision = store.read_pinned(cookie_root, pinned)
    created_at = _timestamp(clock)
    directory = state_root / DIRECTORY
    root_fd = _open_root(directory, create=True)
    invocation = None
    try:
        with _AllocationLock(root_fd, directory):
            invocation = _publish(root_fd, directory, revision.data, pinned, created_at, user_agent, id_factory)
    except BaseException:
        if invocation is None:
            os.close(root_fd)
        else:
            # Releasing the allocation lock failed after publication.
            invocation.close()
        raise
    return invocation


@dataclass(frozen=True)
class CleanupResult:
    """Counts of named candidates only; other entries are ignored (DEC-34)."""
    removed: int
    skipped: int


def cleanup(state_root: Path) -> CleanupResult:
    """Remove abandoned snapshots whose lease can be taken (§3, DEC-34).

    Holds the allocation lock throughout, so no producer can publish while
    eligibility is decided. A missing root succeeds with zero counts and is
    not created. Busy, malformed, unexpected, or lease-less candidates are
    skipped; no age, PID, or canonical-store heuristics. Genuine filesystem
    or lock failures stop cleanup with a path-bearing error (DEC-27).
    """
    directory = state_root / DIRECTORY
    root_fd = _open_root(directory, create=False)
    if root_fd is None:
        return CleanupResult(0, 0)
    removed = skipped = 0
    try:
        with _AllocationLock(root_fd, directory):
            try:
                names = sorted(os.listdir(root_fd))
            except OSError as error:
                raise _failure(directory, error) from None
            for name in names:
                if store.REVISION.fullmatch(name):
                    invocation_id, staging = name, False
                elif name.startswith(STAGING) and store.REVISION.fullmatch(name[len(STAGING):]):
                    invocation_id, staging = name[len(STAGING):], True
                else:
                    continue
                if _clean(root_fd, directory, name, invocation_id, staging):
                    removed += 1
                else:
                    skipped += 1
    finally:
        os.close(root_fd)
    return CleanupResult(removed, skipped)


def _clean(root_fd: int, directory: Path, name: str, invocation_id: str, staging: bool) -> bool:
    """Remove one candidate while holding its lease; False preserves it.

    A published candidate has exactly the three regular files and valid
    metadata for its ID. Interrupted staging may lack the jar or metadata
    and its metadata is not parsed, but it still needs a safely opened,
    empty, regular lease. Cookie contents are never read: downloader
    writeback may have changed them.
    """
    path = directory / name
    dir_fd = lease_fd = None
    try:
        try:
            dir_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                return False
            raise
        identity = _identity(os.fstat(dir_fd))
        children = set(os.listdir(dir_fd))
        if LEASE not in children or not children <= set(FILES) or (not staging and children != set(FILES)):
            return False
        for child in children:
            if not stat.S_ISREG(os.stat(child, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return False
        try:
            lease_fd = os.open(LEASE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        except OSError as error:
            if error.errno == errno.ELOOP:
                return False
            raise
        lease = os.fstat(lease_fd)
        if not stat.S_ISREG(lease.st_mode) or lease.st_size != 0:
            return False
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not staging and not _valid_metadata(dir_fd, path / METADATA, invocation_id):
            return False
        # Both identities must still name what was inspected and locked.
        if (_identity(os.stat(name, dir_fd=root_fd, follow_symlinks=False)) != identity
                or _identity(os.stat(LEASE, dir_fd=dir_fd, follow_symlinks=False)) != _identity(lease)):
            return False
        _remove_owned(root_fd, directory, name, dir_fd, identity, _identity(lease))
        return True
    except FileNotFoundError:
        # A child vanished while inspecting: not provably abandoned.
        return False
    except OSError as error:
        raise _failure(path, error) from None
    finally:
        # The lease is released only after removal has finished.
        _close_all(path, (lease_fd, dir_fd))


def _valid_metadata(dir_fd: int, path: Path, invocation_id: str) -> bool:
    fd = os.open(METADATA, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        data = b''
        while len(data) <= METADATA_LIMIT:
            chunk = os.read(fd, METADATA_LIMIT + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > METADATA_LIMIT:
        return False
    try:
        parse_metadata(data, path, invocation_id)
    except DownloadinuxError:
        return False
    return True
