"""Real SQLite and subprocess ownership evidence for DEC-35/41/45."""
from datetime import datetime, timezone
import errno
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

from downloadinux import job_store as js, cookie_snapshot as cs
from downloadinux.errors import DownloadinuxError

SRC = Path(__file__).resolve().parents[1] / 'src'
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def open_store(root, **kwargs):
    store = js.open_store(root, clock=kwargs.get('clock', lambda: NOW))
    assert isinstance(store, js.JobStore)
    return store


def test_missing_root_stays_missing(tmp_path):
    root = tmp_path / 'absent'
    with pytest.raises(DownloadinuxError, match=str(root)):
        js.open_store(root)
    assert not root.exists()


def test_schema_modes_symlink_root_and_reopen(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o755)
    (root / 'unrelated').write_bytes(b'keep')
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    root_mode = root.stat().st_mode
    unrelated = (root / 'unrelated').stat().st_mode
    previous = os.umask(0)
    try:
        with open_store(alias) as store:
            c = store._connection
            assert store.path == alias / 'jobs' / 'jobs.sqlite3'
            assert c.execute('PRAGMA user_version').fetchone() == (1,)
            assert c.execute('PRAGMA journal_mode').fetchone() == ('delete',)
            assert c.execute('PRAGMA synchronous').fetchone() == (2,)
            assert c.execute('PRAGMA foreign_keys').fetchone() == (1,)
            assert c.execute('SELECT * FROM lanes ORDER BY preset').fetchall() == [('gallery', None), ('video', None)]
            assert c.execute('SELECT * FROM queue_counter').fetchall() == [(1, 0)]
            assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with store._transaction():
                c.execute('INSERT INTO submissions(created_at) VALUES (?)', ('2026-10-11T12:00:00Z',))
                journal = store.path.with_name(js.DATABASE + '-journal')
                assert journal.stat().st_mode & 0o777 == 0o600
            identity = (root / 'jobs' / js.LOCK).stat().st_ino
        for name in ('jobs', 'jobs/logs'):
            assert (root / name).stat().st_mode & 0o777 == 0o700
            (root / name).chmod(0o777)
        for name in (js.LOCK, js.DATABASE):
            assert (root / 'jobs' / name).stat().st_mode & 0o777 == 0o600
            (root / 'jobs' / name).chmod(0o666)
        with open_store(alias) as store:
            assert store._connection.execute('SELECT COUNT(*) FROM submissions').fetchone() == (1,)
            assert (root / 'jobs' / js.LOCK).stat().st_ino == identity
        assert root.stat().st_mode == root_mode
        assert (root / 'unrelated').stat().st_mode == unrelated
        assert all(p.stat().st_mode & 0o777 == mode for p, mode in
                   [(root / 'jobs', 0o700), (root / 'jobs/logs', 0o700),
                    (root / 'jobs' / js.LOCK, 0o600), (root / 'jobs' / js.DATABASE, 0o600)])
        assert not list((root / 'jobs').glob('*-wal'))
        assert not list((root / 'jobs').glob('*-shm'))
    finally:
        os.umask(previous)


@pytest.mark.parametrize('name', ['jobs', 'jobs/logs', 'jobs/.worker.lock', 'jobs/jobs.sqlite3',
                                  'jobs/jobs.sqlite3-journal'])
@pytest.mark.parametrize('kind', ['symlink', 'wrong-type', 'fifo'])
def test_managed_paths_refuse_foreign_types(tmp_path, name, kind):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'foreign'
    target.write_bytes(b'SENTINEL')
    before = target.stat()
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(path)
    elif name in ('jobs', 'jobs/logs'):
        path.write_bytes(b'SENTINEL')
    else:
        path.mkdir()
    with pytest.raises(DownloadinuxError, match=str(path)) as error:
        js.open_store(tmp_path)
    assert 'SENTINEL' not in str(error.value)
    assert target.read_bytes() == b'SENTINEL'
    assert target.stat().st_mode == before.st_mode


OWNER = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from downloadinux import job_store as js
with js.open_store(Path(sys.argv[2])) as store:
    print('owned', flush=True)
    sys.stdin.read()
'''
CONTENDER = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from downloadinux import job_store as js
def forbidden(*args, **kwargs):
    raise AssertionError('contender opened SQLite')
js.sqlite3.connect = forbidden
assert js.open_store(Path(sys.argv[2])) is js.BUSY
print('busy')
'''


def test_subprocess_ownership_and_release(tmp_path):
    owner = subprocess.Popen([sys.executable, '-I', '-c', OWNER, str(SRC), str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == 'owned'
        db = tmp_path / 'jobs' / js.DATABASE
        before = db.read_bytes()
        lock_identity = (tmp_path / 'jobs' / js.LOCK).stat().st_ino
        result = subprocess.run([sys.executable, '-I', '-c', CONTENDER, str(SRC), str(tmp_path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'busy\n'
        assert db.read_bytes() == before
        assert list((tmp_path / 'jobs/logs').iterdir()) == []
        owner.communicate('', timeout=10)
        assert owner.returncode == 0
        with open_store(tmp_path):
            assert (tmp_path / 'jobs' / js.LOCK).stat().st_ino == lock_identity
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate()


def test_busy_before_initialization(tmp_path, monkeypatch):
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    (jobs / js.LOCK).touch()
    fd = os.open(jobs / js.LOCK, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(js.sqlite3, 'connect', lambda *a, **k: pytest.fail('opened before ownership'))
        assert js.open_store(tmp_path) is js.BUSY
        assert sorted(p.name for p in jobs.iterdir()) == [js.LOCK]
    finally:
        os.close(fd)


@pytest.mark.parametrize('module', [js, cs])
def test_reopened_lock_contract_and_identity_failure(tmp_path, monkeypatch, module):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_open, real_flock = os.open, fcntl.flock
    calls, closed = [], []
    real_close = os.close
    def opener(path, flags, mode=0o777, *, dir_fd=None):
        result = real_open(path, flags, mode, dir_fd=dir_fd)
        calls.append((result, bool(flags & os.O_CREAT)))
        return result
    def closer(value):
        closed.append(value)
        real_close(value)
    monkeypatch.setattr(os, 'open', opener)
    monkeypatch.setattr(os, 'close', closer)
    identity = module._create_regular(fd, 'lock', exclusive=False)
    assert calls[-1][1] is True and calls[-1][0] in closed
    lock = module._reopen(fd, 'lock', identity)
    assert calls[-1] == (lock, False)
    real_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    real_close(lock)
    assert (tmp_path / 'lock').stat().st_mode & 0o777 == 0o600
    before = len(closed)
    with pytest.raises(module._NotOwned):
        module._reopen(fd, 'lock', (-1, -1))
    assert len(closed) == before + 1
    real_close(fd)


def test_connect_happens_under_reopened_lock(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    observed = []
    def connect(*a, **k):
        fd = os.open(tmp_path / 'jobs' / js.LOCK, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        observed.append(True)
        return real_connect(*a, **k)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with open_store(tmp_path):
        pass
    assert observed == [True]


@pytest.mark.parametrize('failure', ['connect', 'schema', 'pragmas'])
def test_failed_open_closes_connection_before_owner_and_rolls_back(tmp_path, monkeypatch, failure):
    real_connect, real_close = sqlite3.connect, os.close
    events = []
    class Connection(sqlite3.Connection):
        def execute(self, sql, *a):
            if failure == 'schema' and sql.startswith('CREATE TABLE jobs'):
                # Real SQL error after the first CREATE, not a simulated rollback.
                return super().execute('CREATE TABLE submissions(id)')
            if failure == 'pragmas' and sql == 'PRAGMA foreign_keys=ON':
                return super().execute('PRAGMA foreign_keys=OFF')
            return super().execute(sql, *a)
        def close(self):
            events.append('connection closed')
            return super().close()
    def connect(*a, **k):
        if failure == 'connect':
            return real_connect(tmp_path / 'absent' / 'db')
        return real_connect(*a, **k, factory=Connection)
    def close(fd):
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                events.append('file descriptor closed')
        finally:
            real_close(fd)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(DownloadinuxError, match='jobs.sqlite3'):
        js.open_store(tmp_path)
    if failure != 'connect':
        assert events[-2:] == ['connection closed', 'file descriptor closed']
    monkeypatch.undo()
    c = real_connect(tmp_path / 'jobs' / js.DATABASE)
    assert c.execute('PRAGMA user_version').fetchone() == (0,)
    assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    c.close()
    with open_store(tmp_path):
        pass


@pytest.mark.parametrize('damage', ['corrupt', 'unsupported', 'unversioned', 'table', 'column', 'index',
                                    'lanes', 'counter'])
def test_damaged_stores_fail_without_repair_or_value_leak(tmp_path, damage):
    with open_store(tmp_path) as store:
        c = store._connection
        if damage == 'unsupported':
            c.execute('PRAGMA user_version=99')
        elif damage == 'unversioned':
            c.execute('PRAGMA user_version=0')
        elif damage == 'table':
            c.execute('DROP TABLE lanes')
        elif damage == 'column':
            c.execute('ALTER TABLE jobs DROP COLUMN error')
        elif damage == 'index':
            c.execute('DROP INDEX active_jobs')
        elif damage == 'lanes':
            c.execute("DELETE FROM lanes WHERE preset='video'")
        elif damage == 'counter':
            c.execute('DELETE FROM queue_counter')
    db = tmp_path / 'jobs' / js.DATABASE
    if damage == 'corrupt':
        db.write_bytes(b'SENTINEL corrupt database')
    before = db.read_bytes()
    with pytest.raises(DownloadinuxError, match=str(db)) as error:
        js.open_store(tmp_path)
    assert 'SENTINEL' not in str(error.value)
    assert db.read_bytes() == before
    # Failed opening released its descriptor even on corruption.
    fd = os.open(tmp_path / 'jobs' / js.LOCK, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_existing_empty_sqlite_is_initialized(tmp_path):
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    sqlite3.connect(jobs / js.DATABASE).close()
    with open_store(tmp_path) as store:
        assert store._connection.execute('PRAGMA user_version').fetchone() == (1,)


@pytest.mark.parametrize('module', [js, cs])
@pytest.mark.parametrize('phase', ['create', 'reopen'])
def test_lock_fstat_failure_closes_descriptor(tmp_path, monkeypatch, module, phase):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    identity = module._create_regular(fd, 'lock', exclusive=False)
    closed = []
    real_close = os.close
    def fail(value):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    def close(value):
        closed.append(value)
        real_close(value)
    monkeypatch.setattr(os, 'fstat', fail)
    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(OSError):
        if phase == 'create':
            module._create_regular(fd, 'lock', exclusive=False)
        else:
            module._reopen(fd, 'lock', identity)
    assert len(closed) == 1
    real_close(fd)


def test_real_filesystem_failure_is_safe_and_releases_owner(tmp_path):
    if os.getuid() == 0:
        pytest.skip('root bypasses directory permissions')
    # Existing logs directory cannot be opened; ownership was already acquired.
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    logs = jobs / 'logs'
    logs.mkdir(mode=0)
    try:
        with pytest.raises(DownloadinuxError, match=str(logs)) as error:
            js.open_store(tmp_path)
        assert 'Permission denied' in str(error.value)
        fd = os.open(jobs / js.LOCK, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        logs.chmod(0o700)


def test_lock_os_failure_normalized(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    monkeypatch.setattr(fcntl, 'flock', fail)
    with pytest.raises(DownloadinuxError, match=r'\.worker.lock: Input/output error'):
        js.open_store(tmp_path)
    monkeypatch.undo()
    with open_store(tmp_path):
        pass


def contents(store):
    c = store._connection
    return tuple(c.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                 for table in ('submissions', 'jobs', 'queue_counter'))


def test_submissions_initial_fields_order_duplicates_and_no_material(tmp_path, monkeypatch):
    with open_store(tmp_path) as store:
        def forbidden(*a, **k):
            pytest.fail('submission touched material')
        monkeypatch.setattr(Path, 'read_bytes', forbidden)
        first = store.submit('video', ['a', 'b', 'a', 'c'], lane='video', source='alpha',
                             revision='a' * 32, user_agent='literal')
        assert first.submission == 1
        assert [(j.id, j.url) for j in first.created] == [(1, 'a'), (2, 'b'), (3, 'c')]
        assert first.duplicates == ()
        c = store._connection
        c.row_factory = sqlite3.Row
        jobs = [dict(row) for row in c.execute('SELECT * FROM jobs ORDER BY id')]
        c.row_factory = None
        for order, job in enumerate(jobs, 1):
            assert job == dict(id=order, submission=1, preset='video', url='abc'[order - 1], lane='video',
                              source='alpha', revision='a' * 32, user_agent='literal', state='queued', wait=None,
                              attempt=1, cancel_requested=0, created_at='2026-10-11T12:00:00Z',
                              queued_at='2026-10-11T12:00:00Z', started_at=None, finished_at=None,
                              exit_status=None, error=None, queue_order=order)
        second = store.submit('video', ['b', 'new', 'a', 'new', 'other'], user_agent='changed')
        assert second.submission == 2
        assert [(j.id, j.url) for j in second.created] == [(4, 'new'), (5, 'other')]
        assert [(j.id, j.url) for j in second.duplicates] == [(2, 'b'), (1, 'a')]
        assert c.execute('SELECT * FROM jobs WHERE id<=3 ORDER BY id').fetchall() == [tuple(j.values()) for j in jobs]
        third = store.submit('video', ['b', 'b', 'a'], lane='video', source='beta', revision='b' * 32)
        assert third.submission == 3 and third.created == ()
        assert [(j.id, j.url) for j in third.duplicates] == [(2, 'b'), (1, 'a')]
        assert c.execute('SELECT value FROM queue_counter').fetchone() == (5,)
        assert sorted(p.name for p in (tmp_path / 'jobs').iterdir()) == [js.LOCK, js.DATABASE, 'logs']
        assert list((tmp_path / 'jobs/logs').iterdir()) == []
        assert not (tmp_path / 'cookie-invocations').exists()
        assert not (tmp_path / 'cookies').exists()


@pytest.mark.parametrize('active', ['queued', 'running'])
@pytest.mark.parametrize('original_lane,new_lane', [(None, 'video'), ('video', None), ('video', 'video'), (None, None)])
def test_active_duplicates_preserve_every_field_across_queues(tmp_path, active, original_lane, new_lane):
    with open_store(tmp_path) as store:
        first = store.submit('video', ['url'], lane=original_lane, source='alpha', revision='a' * 32, user_agent='old')
        c = store._connection
        c.execute('''UPDATE jobs SET state=?, wait=?, attempt=2, cancel_requested=1,
                     started_at=?, error=? WHERE id=?''',
                  (active, '{"reason":"vpn"}' if active == 'queued' else None,
                   None if active == 'queued' else '2026-10-11T12:00:00Z', 'diagnostic', first.created[0].id))
        before = c.execute('SELECT * FROM jobs').fetchall()
        second = store.submit('video', ['url'], lane=new_lane, source='beta', revision='b' * 32, user_agent='new')
        assert second.created == () and second.duplicates == first.created
        assert c.execute('SELECT * FROM jobs').fetchall() == before


@pytest.mark.parametrize('terminal', js.STATES[2:])
def test_terminal_states_allow_resubmission(tmp_path, terminal):
    with open_store(tmp_path) as store:
        first = store.submit('video', ['url'])
        store._connection.execute('UPDATE jobs SET state=? WHERE id=?', (terminal, first.created[0].id))
        second = store.submit('video', ['url'], lane='video')
        assert second.duplicates == ()
        assert second.created[0].id > first.created[0].id


def test_exact_binary_comparison_and_presets(tmp_path):
    urls = ['https://EXAMPLE/a', 'https://example/a', ' https://example/a', 'https://example/a ',
            'é', 'e\u0301', '\t', "quotes'\"\\"]
    with open_store(tmp_path) as store:
        first = store.submit('video', urls)
        second = store.submit('gallery', urls)
        assert [j.url for j in first.created] == [j.url for j in second.created] == urls
        assert second.duplicates == ()
        assert store.submit('video', list(reversed(urls))).duplicates == tuple(reversed(first.created))


INVALID = [dict(urls=[]), dict(urls='url'), dict(urls=b'url'), dict(urls={'url'}), dict(urls=(u for u in ['a'])),
           dict(urls=['valid', '']), dict(urls=['valid', None]), dict(urls=['valid', True]),
           dict(urls=['valid', 1]), dict(urls=['valid', 'bad\r']), dict(urls=['valid', 'bad\n']),
           dict(urls=['valid', 'bad\0']), dict(preset='mp4'), dict(preset=True), dict(preset=[]),
           dict(lane='gallery'), dict(lane='immediate'), dict(lane=True), dict(lane=[]),
           dict(source='alpha'), dict(revision='a' * 32), dict(source='', revision='a' * 32),
           dict(source='Alpha', revision='a' * 32), dict(source='é', revision='a' * 32),
           dict(source='a' * 65, revision='a' * 32), dict(source=True, revision='a' * 32),
           dict(source='alpha', revision='A' * 32), dict(source='alpha', revision='a' * 31),
           dict(source='alpha', revision=True), dict(user_agent=None), dict(user_agent=True),
           dict(user_agent='bad\r'), dict(user_agent='bad\n'), dict(user_agent='bad\0')]


@pytest.mark.parametrize('invalid', INVALID)
def test_complete_prevalidation_publishes_nothing(tmp_path, invalid):
    with open_store(tmp_path) as store:
        before = contents(store)
        args = dict(preset='video', urls=['good'], user_agent='')
        args.update(invalid)
        with pytest.raises(DownloadinuxError):
            store.submit(**args)
        assert contents(store) == before
        assert not store._connection.in_transaction


@pytest.mark.parametrize('failure', ['mid-insert', 'commit', 'text-encoding'])
def test_real_transaction_failures_rollback_all_mutations(tmp_path, failure):
    with open_store(tmp_path) as store:
        c = store._connection
        store.submit('gallery', ['existing'])
        if failure == 'mid-insert':
            c.execute('''CREATE TRIGGER fail_job BEFORE INSERT ON jobs WHEN NEW.url='SENTINEL'
                         BEGIN SELECT RAISE(ABORT, 'SENTINEL secret user-agent'); END''')
        elif failure == 'commit':
            c.execute('''CREATE TABLE deferred_failure (submission INTEGER REFERENCES submissions(id)
                         DEFERRABLE INITIALLY DEFERRED)''')
            c.execute('''CREATE TRIGGER fail_commit AFTER INSERT ON jobs
                         BEGIN INSERT INTO deferred_failure VALUES(-1); END''')
        before = contents(store)
        urls = ['first', '\ud800'] if failure == 'text-encoding' else ['first', 'SENTINEL']
        with pytest.raises(DownloadinuxError, match='jobs.sqlite3') as error:
            store.submit('video', urls, user_agent='SENTINEL secret user-agent')
        assert 'SENTINEL' not in str(error.value)
        assert contents(store) == before
        assert not c.in_transaction
        if failure == 'mid-insert':
            c.execute('DROP TRIGGER fail_job')
        elif failure == 'commit':
            assert c.execute('SELECT * FROM deferred_failure').fetchall() == []
            c.execute('DROP TRIGGER fail_commit')
            c.execute('DROP TABLE deferred_failure')
        success = store.submit('video', ['after'])
        assert success.created and contents(store)[2] == [(1, 2)]


def test_reopen_durable_ids_queue_order_and_backward_clock(tmp_path):
    with open_store(tmp_path) as store:
        first = store.submit('video', ['one', 'two'], source='a' * 64, revision='0' * 32,
                             user_agent='literal \t\\"é')
        duplicate = store.submit('video', ['one'])
        before = contents(store)
        assert duplicate.submission > first.submission
        assert duplicate.created == ()
    with open_store(tmp_path, clock=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)) as store:
        assert contents(store) == before
        assert store.submit('video', ['one']).duplicates == first.created[:1]
        c = store._connection
        # A deleted committed job ID must never be reused.
        c.execute("UPDATE jobs SET state='succeeded'")
        c.execute('DELETE FROM jobs WHERE id=2')
        next_result = store.submit('video', ['new'])
        assert next_result.submission > duplicate.submission
        assert next_result.created[0].id > first.created[-1].id
        orders = c.execute('SELECT queue_order, queued_at FROM jobs ORDER BY id').fetchall()
        assert orders == [(1, '2026-10-11T12:00:00Z'), (3, '2020-01-01T00:00:00Z')]
        assert c.execute('SELECT value FROM queue_counter').fetchone() == (3,)


def test_active_partial_unique_index_rejects_real_duplicate(tmp_path):
    with open_store(tmp_path) as store:
        store.submit('video', ['one'])
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute('''INSERT INTO jobs
                (submission,preset,url,user_agent,state,attempt,cancel_requested,created_at,queued_at,queue_order)
                SELECT submission,preset,url,user_agent,state,attempt,cancel_requested,created_at,queued_at,queue_order
                FROM jobs''')
