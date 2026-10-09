"""Lock-free, standard-library source-store reader (DEC-28, DEC-30).

The root is supplied explicitly. Reading never migrates, creates directories,
loads settings, or changes state; immutable revisions outlive snapshots.
"""
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping

from . import cookiejar
from .errors import DownloadinuxError

NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}', re.ASCII)
REVISION = re.compile(r'[0-9a-f]{32}', re.ASCII)
TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', re.ASCII)


def invalid(path: Path, field: str) -> DownloadinuxError:
    return DownloadinuxError(f'{path}: invalid {field}; repair the source store or retry the operation')


def validate_name(name: str, path: Path) -> None:
    if type(name) is not str or not NAME.fullmatch(name):
        raise invalid(path, 'source name')


def validate_revision(revision: str, path: Path) -> None:
    if type(revision) is not str or not REVISION.fullmatch(revision):
        raise invalid(path, 'revision')


@dataclass(frozen=True)
class Source:
    kind: str
    revision: str
    refreshed_at: str
    path: str | None = None
    browser: str | None = None
    profile: str | None = None
    keyring: str | None = None
    container: str | None = None

    def record(self) -> dict:
        origin = dict(path=self.path) if self.kind == 'file' else dict(browser=self.browser, profile=self.profile, keyring=self.keyring, container=self.container)
        return dict(kind=self.kind, **origin, revision=self.revision, refreshed_at=self.refreshed_at)


@dataclass(frozen=True)
class Snapshot:
    default: str | None
    sources: Mapping[str, Source]

    def record(self) -> dict:
        return dict(version=1, default=self.default, sources={name: self.sources[name].record() for name in sorted(self.sources)})


def source_record(record: object, path: Path) -> Source:
    if type(record) is not dict:
        raise invalid(path, 'source record')
    common = {'kind', 'revision', 'refreshed_at'}
    kind = record.get('kind')
    if kind == 'file':
        fields = common | {'path'}
    elif kind == 'browser':
        fields = common | {'browser', 'profile', 'keyring', 'container'}
    else:
        raise invalid(path, 'source kind')
    if set(record) != fields:
        raise invalid(path, 'source fields')
    validate_revision(record['revision'], path)
    timestamp = record['refreshed_at']
    if type(timestamp) is not str or not TIMESTAMP.fullmatch(timestamp):
        raise invalid(path, 'refresh timestamp')
    try:
        datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise invalid(path, 'refresh timestamp') from None
    if kind == 'file':
        origin = record['path']
        if type(origin) is not str or not Path(origin).is_absolute() or '\x00' in origin:
            raise invalid(path, 'file origin')
    else:
        if type(record['browser']) is not str or record['browser'] not in ('firefox', 'chrome', 'chromium'):
            raise invalid(path, 'browser')
        for field in ('profile', 'keyring', 'container'):
            if record[field] is not None and type(record[field]) is not str:
                raise invalid(path, 'browser selector')
    return Source(**record)


def validate_record(record: object, path: Path) -> Snapshot:
    if type(record) is not dict or set(record) != {'version', 'default', 'sources'}:
        raise invalid(path, 'registry fields')
    if type(record['version']) is not int or record['version'] != 1:
        raise invalid(path, 'registry version')
    if type(record['sources']) is not dict:
        raise invalid(path, 'sources')
    sources = {}
    for name, source in record['sources'].items():
        validate_name(name, path)
        sources[name] = source_record(source, path)
    default = record['default']
    if default is not None and (type(default) is not str or default not in sources):
        raise invalid(path, 'default')
    return Snapshot(default, MappingProxyType(sources))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def revision_path(root: Path, name: str, revision: str) -> Path:
    validate_name(name, root)
    validate_revision(revision, root)
    return root / 'sources' / name / f'{revision}.txt'


def load_revision(root: Path, name: str, revision: str) -> cookiejar.Material:
    path = revision_path(root, name, revision)
    try:
        # A configured root can be a symlink; managed children must not be.
        for parent in (root / 'sources', path.parent):
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise invalid(path, 'managed directory')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise invalid(path, 'managed revision file')
            data = handle.read()
    except OSError as error:
        raise DownloadinuxError(f'{path}: {error.strerror or "cannot read managed revision"}') from None
    return cookiejar.parse(data, path)


def read(root: Path) -> Snapshot | None:
    """None denotes missing registry, separately from an empty anonymous store."""
    path = root / 'sources.json'
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DownloadinuxError(f'{path}: {error.strerror or "cannot read registry"}') from None
    try:
        record = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
    except Exception:
        raise invalid(path, 'registry JSON') from None
    snapshot = validate_record(record, path)
    for name, source in snapshot.sources.items():
        load_revision(root, name, source.revision)
    return snapshot


def source_metadata(snapshot: Snapshot, name: str) -> dict:
    return dict(**snapshot.sources[name].record(), name=name, is_default=snapshot.default == name)


def list_metadata(snapshot: Snapshot) -> dict:
    return dict(default=snapshot.default, sources=[source_metadata(snapshot, name) for name in sorted(snapshot.sources)])


def inspect(root: Path, snapshot: Snapshot, name: str, *, now: float | None = None) -> dict:
    validate_name(name, root / 'sources.json')
    if name not in snapshot.sources:
        raise DownloadinuxError(f'{root / "sources.json"}: source {name} is not registered; choose a listed source')
    return dict(source=source_metadata(snapshot, name), **cookiejar.summary(load_revision(root, name, snapshot.sources[name].revision), now=now))
