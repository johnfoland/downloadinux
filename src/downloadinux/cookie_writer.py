"""Host-only source publication, with one registry commit point (DEC-28).

Extraction is outside this boundary. Callers carry the original Source record
(or None for expected absence); conflicting writers must retry explicitly.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Callable
from uuid import uuid4

from . import cookie_store as store, cookiejar, files
from .errors import DownloadinuxError


def _failure(path: Path, error: OSError) -> DownloadinuxError:
    return DownloadinuxError(f'{path}: {error.strerror or "filesystem operation failed"}')


def _directory(path: Path, *, configured_root: bool = False) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR((path.stat() if configured_root else path.lstat()).st_mode):
            raise store.invalid(path, 'managed directory')
        path.chmod(0o700)
    except OSError as error:
        raise _failure(path, error) from None


@contextmanager
def locked(root: Path):
    """Every writer holds this host lock through the complete registry commit."""
    _directory(root, configured_root=True)
    path = root / '.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise store.invalid(path, 'writer lock')
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
    except OSError as error:
        raise _failure(path, error) from None


def _write(path: Path, text: str, *, replace: bool) -> None:
    try:
        # Same-directory staging, fsync, no-clobber fallback and ownership-aware
        # cleanup remain in the shared writer (DEC-24, DEC-27).
        files.write_atomic(path, text, 0o600, replace=replace)
    except OSError as error:
        raise _failure(path, error) from None


def _current(root: Path) -> tuple[store.Snapshot, bool]:
    snapshot = store.read(root)
    return (snapshot, True) if snapshot is not None else (store.validate_record(dict(version=1, default=None, sources={}), root / 'sources.json'), False)


def _commit(root: Path, record: dict, *, replace: bool) -> store.Snapshot:
    snapshot = store.validate_record(record, root / 'sources.json')
    _write(root / 'sources.json', json.dumps(snapshot.record(), sort_keys=True) + '\n', replace=replace)
    return snapshot


def initialize(root: Path) -> store.Snapshot:
    """Create an anonymous empty registry only if absent; never migrate."""
    with locked(root):
        snapshot = store.read(root)
        if snapshot is not None:
            return snapshot
        return _commit(root, dict(version=1, default=None, sources={}), replace=False)


def _prepare(root: Path, name: str, material: cookiejar.Material, origin: dict,
             revision_factory: Callable[[], str], clock: Callable[[], datetime]):
    registry = root / 'sources.json'
    store.validate_name(name, registry)
    revision = revision_factory()
    path = store.revision_path(root, name, revision)
    instant = clock()
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise store.invalid(registry, 'refresh clock')
    timestamp = instant.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    # Unknown/kind-inappropriate origin fields must not be silently discarded.
    if type(origin) is not dict or {'revision', 'refreshed_at'} & origin.keys():
        raise store.invalid(registry, 'source origin')
    source = store.source_record(dict(origin, revision=revision, refreshed_at=timestamp), registry)
    text = cookiejar.serialize(material, path)
    return source, path, text


def _publish_locked(root: Path, name: str, source: store.Source, path: Path,
                    text: str, current: store.Snapshot, exists: bool,
                    *, select: bool) -> store.Snapshot:
    """Caller holds the host lock; all publication paths share this transaction."""
    _directory(root / 'sources')
    _directory(path.parent)
    _write(path, text, replace=False)
    record = current.record()
    record['sources'][name] = source.record()
    if select:
        record['default'] = name
    return _commit(root, record, replace=exists)


def publish(root: Path, name: str, material: cookiejar.Material, origin: dict,
            *, expected: store.Source | None, select: bool = False,
            revision_factory: Callable[[], str] = lambda: uuid4().hex,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> store.Source:
    """Publish a fresh immutable copy, then origin/revision/default atomically.

    Safe unreferenced revisions may remain on failure. Never remove immutable
    material or guess at reservation ownership; DEC-27 cleanup lives in files.
    """
    source, path, text = _prepare(root, name, material, origin, revision_factory, clock)
    with locked(root):
        current, exists = _current(root)
        if current.sources.get(name) != expected:
            raise DownloadinuxError(f'{root / "sources.json"}: source {name} changed; retry the operation')
        _publish_locked(root, name, source, path, text, current, exists, select=select)
        return source


def ensure_store(root: Path, *,
                 revision_factory: Callable[[], str] = lambda: uuid4().hex,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> store.Snapshot:
    """Migrate once on host source access (Named cookie sources §5, DEC-33).

    The reader remains mutation-free. Recheck absence and validate legacy
    material inside the same lock as publication, without nested acquisition.
    """
    current = store.read(root)
    if current is not None:
        return current
    with locked(root):
        current, exists = _current(root)
        if exists:
            return current
        legacy = root / 'cookies.txt'
        try:
            try:
                data = legacy.read_bytes()
            except FileNotFoundError:
                data = cookiejar.HEADER.encode()
            except OSError as error:
                raise _failure(legacy, error) from None
            material = cookiejar.parse(data, legacy)
        except DownloadinuxError as error:
            raise DownloadinuxError(
                f'{error}; repair or move aside {legacy} before importing valid cookie data'
            ) from None
        if not material.jar:
            return _commit(root, current.record(), replace=False)
        source, path, text = _prepare(
            root, 'legacy', material, dict(kind='file', path=os.path.abspath(legacy)),
            revision_factory, clock,
        )
        return _publish_locked(root, 'legacy', source, path, text, current, False, select=True)


def set_default(root: Path, name: str | None) -> store.Snapshot:
    """Default-only changes retain every revision and refresh timestamp."""
    if name is not None:
        store.validate_name(name, root / 'sources.json')
    with locked(root):
        current, exists = _current(root)
        if name is not None and name not in current.sources:
            raise DownloadinuxError(f'{root / "sources.json"}: source {name} is not registered; choose a listed source')
        record = current.record()
        record['default'] = name
        return _commit(root, record, replace=exists)
