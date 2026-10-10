from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import multiprocessing as mp
import os
from pathlib import Path
import stat

import pytest
from downloadinux import cookiejar, cookie_store as store, cookie_writer as writer, files
from downloadinux.errors import DownloadinuxError

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
REVISION = '1' * 32
DATA = (cookiejar.HEADER +
        '#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tCOOKIE_CANARY\n'
        'youtube.com\tFALSE\t/\tFALSE\t1\tSAPISID\tCOOKIE_CANARY\n').encode()


def ensure(root, revision=REVISION):
    return writer.ensure_store(root, revision_factory=lambda: revision, clock=lambda: NOW)


def legacy(root, data=DATA):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'cookies.txt'
    path.write_bytes(data)
    path.chmod(0o644)
    return path


@pytest.mark.parametrize('data', [None, cookiejar.HEADER.encode(), b'\xef\xbb\xbf# Netscape HTTP Cookie File\r\n'])
def test_missing_or_empty_anonymous(tmp_path, data):
    root = tmp_path / 'absent'
    if data is not None:
        old = legacy(root, data)
    assert store.read(root) is None
    snapshot = ensure(root)
    assert snapshot.record() == dict(version=1, default=None, sources={})
    assert not (root / 'sources').exists()
    if data is not None:
        assert old.read_bytes() == data and stat.S_IMODE(old.stat().st_mode) == 0o644
    before = (root / 'sources.json').read_bytes()
    assert ensure(root).record() == snapshot.record()
    assert (root / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('data', [DATA, b'\xef\xbb\xbf' + DATA.replace(b'\n', b'\r\n')])
def test_nonempty_complete_preserved_and_idempotent(tmp_path, data):
    old = legacy(tmp_path, data)
    checkout = legacy(tmp_path / 'checkout')
    previous_umask = os.umask(0)
    try:
        snapshot = ensure(tmp_path)
    finally:
        os.umask(previous_umask)
    source = snapshot.sources['legacy']
    assert snapshot.default == 'legacy'
    assert source.record() == dict(kind='file', path=str(old), revision=REVISION, refreshed_at='2026-10-09T12:00:00Z')
    loaded = store.load_revision(tmp_path, 'legacy', source.revision)
    assert cookiejar.serialize(loaded, old) == cookiejar.serialize(cookiejar.parse(data, old), old)
    records = list(loaded.jar)
    assert records[0].has_nonstandard_attr('HTTPOnly') and records[0].secure
    assert cookiejar.summary(loaded, now=2)['session'] == 1
    assert cookiejar.summary(loaded, now=2)['expired'] == 1
    for path in (tmp_path, tmp_path / 'sources', tmp_path / 'sources/legacy'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (tmp_path / '.lock', tmp_path / 'sources.json', store.revision_path(tmp_path, 'legacy', REVISION)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert old.read_bytes() == data and stat.S_IMODE(old.stat().st_mode) == 0o644
    assert checkout.read_bytes() == DATA and stat.S_IMODE(checkout.stat().st_mode) == 0o644
    before = (tmp_path / 'sources.json').read_bytes()
    old.write_bytes(b'BROKEN COOKIE_CANARY')
    assert ensure(tmp_path, '2' * 32) == snapshot
    assert (tmp_path / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('data, location', [
    (b'BAD_COOKIE_CANARY', 'line 1: invalid header'),
    (cookiejar.HEADER.encode() + b'COOKIE_CANARY\n', 'line 2: invalid field count'),
    (b'\xef\xbb\xbf' + cookiejar.HEADER.encode() + b'\xffCOOKIE_CANARY\n', 'line 2: invalid UTF-8'),
    ((cookiejar.HEADER + 'Example.com\tFALSE\t/\tFALSE\t0\tn\tCOOKIE_CANARY\n'
      'example.com\tFALSE\t/\tFALSE\t0\tn\tOTHER\n').encode(), 'line 3: invalid duplicate'),
])
def test_invalid_legacy_no_publication(tmp_path, data, location):
    old = legacy(tmp_path, data)
    with pytest.raises(DownloadinuxError) as error:
        ensure(tmp_path)
    assert str(old) in str(error.value) and location in str(error.value)
    assert 'repair or move aside' in str(error.value)
    assert 'COOKIE_CANARY' not in str(error.value) and '--anonymous' not in str(error.value)
    assert store.read(tmp_path) is None and not (tmp_path / 'sources').exists()
    assert old.read_bytes() == data and stat.S_IMODE(old.stat().st_mode) == 0o644
    old.write_bytes(DATA)
    assert ensure(tmp_path).default == 'legacy'


def test_actual_unreadable_legacy(tmp_path):
    old = tmp_path / 'cookies.txt'
    old.mkdir()
    with pytest.raises(DownloadinuxError, match='repair or move aside') as error:
        ensure(tmp_path)
    assert str(old) in str(error.value) and 'directory' in str(error.value).lower()
    assert store.read(tmp_path) is None and old.is_dir()


@pytest.mark.parametrize('named', [False, True])
def test_existing_registry_ignores_bad_legacy(tmp_path, named):
    if named:
        writer.publish(tmp_path, 'work', cookiejar.parse(DATA, Path('fixture')), dict(kind='file', path='/origin'), expected=None, select=True)
    else:
        writer.initialize(tmp_path)
    legacy(tmp_path, b'COOKIE_CANARY')
    before = (tmp_path / 'sources.json').read_bytes()
    assert ensure(tmp_path).default == ('work' if named else None)
    assert (tmp_path / 'sources.json').read_bytes() == before


@pytest.mark.parametrize('damage', ['json', 'missing_revision', 'bad_revision'])
def test_damaged_existing_state_never_reset(tmp_path, damage):
    old = legacy(tmp_path)
    ensure(tmp_path)
    if damage == 'json':
        (tmp_path / 'sources.json').write_bytes(b'COOKIE_CANARY')
    elif damage == 'missing_revision':
        store.revision_path(tmp_path, 'legacy', REVISION).unlink()
    else:
        store.revision_path(tmp_path, 'legacy', REVISION).write_bytes(b'COOKIE_CANARY')
    before = (tmp_path / 'sources.json').read_bytes()
    with pytest.raises(DownloadinuxError):
        ensure(tmp_path)
    assert (tmp_path / 'sources.json').read_bytes() == before and old.read_bytes() == DATA


def _migrate(root, ready, start, result, pause_absence=False):
    if pause_absence:
        real = store.read
        first = True
        def read(path):
            nonlocal first
            snapshot = real(path)
            if first:
                first = False
                ready.set()
                assert start.wait(15)
            return snapshot
        store.read = read
    else:
        ready.set()
        assert start.wait(15)
    result.put(ensure(Path(root)).record())


def finish(processes):
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join()
        assert process.exitcode == 0


def test_independent_migrations_one_publication_no_deadlock(tmp_path):
    legacy(tmp_path)
    ctx = mp.get_context('spawn')
    ready = [ctx.Event(), ctx.Event()]
    start, result = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_migrate, args=(str(tmp_path), event, start, result, True)) for event in ready]
    for process in processes:
        process.start()
    try:
        assert all(event.wait(15) for event in ready)
        start.set()
        snapshots = [result.get(timeout=15) for _ in processes]
        assert snapshots[0] == snapshots[1]
        assert snapshots[0]['default'] == 'legacy'
        assert len(list((tmp_path / 'sources/legacy').glob('*.txt'))) == 1
    finally:
        start.set()
        finish(processes)


@pytest.mark.parametrize('anonymous', [False, True])
def test_locked_absence_recheck_keeps_new_registration_and_default(tmp_path, anonymous):
    legacy(tmp_path)
    ctx = mp.get_context('spawn')
    ready, start, result = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_migrate, args=(str(tmp_path), ready, start, result, True))
    process.start()
    try:
        assert ready.wait(15)
        if anonymous:
            expected = writer.initialize(tmp_path)
        else:
            writer.publish(tmp_path, 'work', cookiejar.parse(DATA, Path('fixture')), dict(kind='file', path='/origin'), expected=None, select=True)
            expected = store.read(tmp_path)
        before = (tmp_path / 'sources.json').read_bytes()
        start.set()
        assert result.get(timeout=15) == expected.record()
        assert (tmp_path / 'sources.json').read_bytes() == before
        assert not (tmp_path / 'sources/legacy').exists()
    finally:
        start.set()
        finish([process])


def _interrupted(root, reached):
    def pause(*args, **kwargs):
        reached.set()
        mp.get_context('spawn').Event().wait(60)
    writer._commit = pause
    ensure(Path(root))


def test_interruption_releases_lock_retains_revision_and_old_jar(tmp_path):
    old = legacy(tmp_path)
    ctx = mp.get_context('spawn')
    reached = ctx.Event()
    process = ctx.Process(target=_interrupted, args=(str(tmp_path), reached))
    process.start()
    try:
        assert reached.wait(15)
        assert store.read(tmp_path) is None
        assert store.load_revision(tmp_path, 'legacy', REVISION).jar
        process.kill()
        process.join(15)
        assert not process.is_alive()
        assert ensure(tmp_path, '2' * 32).default == 'legacy'
        assert store.load_revision(tmp_path, 'legacy', REVISION).jar
        assert old.read_bytes() == DATA
    finally:
        if process.is_alive():
            process.kill()
            process.join()


@pytest.mark.parametrize('publication', ['ordinary', 'migration'])
@pytest.mark.parametrize('stage', ['revision_create', 'revision_write', 'revision_sync',
                                  'registry_create', 'registry_write', 'registry_sync', 'registry_link'])
def test_shared_atomic_writer_oracle(tmp_path, monkeypatch, publication, stage):
    old = legacy(tmp_path)
    real_temp, real_fdopen, real_sync, real_link = files.tempfile.mkstemp, os.fdopen, os.fsync, os.link
    kinds = {}
    def fail(path=None, destination=None):
        raise OSError(errno.EIO, os.strerror(errno.EIO), str(path) if path else None, None, str(destination) if destination else None)
    def temporary(*args, **kwargs):
        kind = 'registry' if kwargs['prefix'].startswith('.sources.json.') else 'revision'
        if stage == kind + '_create':
            fail(Path(kwargs['dir']) / kwargs['prefix'])
        fd, path = real_temp(*args, **kwargs)
        kinds[fd] = kind
        return fd, path
    class FailedWrite:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def fileno(self): return self.handle.fileno()
        def write(self, text): fail()
    def fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        return FailedWrite(handle) if stage == kinds.get(fd, '') + '_write' else handle
    def sync(fd):
        if stage == kinds.get(fd, '') + '_sync': fail()
        return real_sync(fd)
    def link(source, destination):
        if Path(destination).name == 'sources.json' and stage == 'registry_link': fail(source, destination)
        return real_link(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(files.tempfile, 'mkstemp', temporary)
        patch.setattr(os, 'fdopen', fdopen)
        patch.setattr(os, 'fsync', sync)
        patch.setattr(os, 'link', link)
        with pytest.raises(DownloadinuxError) as error:
            if publication == 'migration':
                ensure(tmp_path)
            else:
                writer.publish(tmp_path, 'legacy', cookiejar.parse(DATA, old), dict(kind='file', path=str(old)), expected=None, select=True, revision_factory=lambda: REVISION, clock=lambda: NOW)
    target = tmp_path / 'sources.json' if stage.startswith('registry') else store.revision_path(tmp_path, 'legacy', REVISION)
    assert str(target) in str(error.value) and 'COOKIE_CANARY' not in str(error.value)
    assert store.read(tmp_path) is None and old.read_bytes() == DATA
    assert not list(tmp_path.rglob('*.tmp'))
    assert store.revision_path(tmp_path, 'legacy', REVISION).exists() == stage.startswith('registry')
    assert ensure(tmp_path, '2' * 32).default == 'legacy'


def test_actual_permission_denied_legacy(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root can read mode-000 files')
    old = legacy(tmp_path)
    old.chmod(0)
    try:
        with pytest.raises(DownloadinuxError, match='Permission denied') as error:
            ensure(tmp_path)
        assert str(old) in str(error.value) and 'repair or move aside' in str(error.value)
        assert store.read(tmp_path) is None
        assert stat.S_IMODE(old.stat().st_mode) == 0
    finally:
        old.chmod(0o644)
    assert old.read_bytes() == DATA


def _held_parse(root, reached, release):
    real = cookiejar.parse
    def parse(data, path):
        if path == Path(root) / 'cookies.txt':
            reached.set()
            assert release.wait(15)
        return real(data, path)
    cookiejar.parse = parse
    ensure(Path(root))


def _register(root, attempting, done):
    attempting.set()
    writer.publish(Path(root), 'work', cookiejar.parse(DATA, Path('fixture')), dict(kind='file', path='/origin'), expected=None, select=True)
    done.set()


def test_parse_holds_publication_lock_and_later_registration_preserved(tmp_path):
    legacy(tmp_path)
    ctx = mp.get_context('spawn')
    reached, release, attempting, done = [ctx.Event() for _ in range(4)]
    migration = ctx.Process(target=_held_parse, args=(str(tmp_path), reached, release))
    registration = ctx.Process(target=_register, args=(str(tmp_path), attempting, done))
    migration.start()
    try:
        assert reached.wait(15)
        registration.start()
        assert attempting.wait(15) and not done.wait(.2)
        assert store.read(tmp_path) is None
        release.set()
        assert done.wait(15)
        snapshot = store.read(tmp_path)
        assert set(snapshot.sources) == {'legacy', 'work'} and snapshot.default == 'work'
    finally:
        release.set()
        finish([p for p in (migration, registration) if p.pid is not None])
