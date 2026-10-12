"""Real SQLite and subprocess ownership evidence for DEC-35/41/45."""
from datetime import datetime, timezone
from dataclasses import asdict
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


@pytest.fixture
def populated(tmp_path):
    with open_store(tmp_path) as store:
        for preset in js.PRESETS:
            result = store.submit(preset, [preset + '/' + state for state in js.STATES], lane=preset)
            for reference, state in zip(result.created, js.STATES):
                store._connection.execute('UPDATE jobs SET state=? WHERE id=?', (state, reference.id))
        yield store


@pytest.mark.parametrize('state', [None, *js.STATES])
@pytest.mark.parametrize('preset', [None, *js.PRESETS])
@pytest.mark.parametrize('submission', [None, 1, 2])
def test_every_filter_combination_and_id_order(populated, state, preset, submission):
    records = populated.list()
    expected = [r for r in records if (state is None or r.state == state)
                and (preset is None or r.preset == preset) and (submission is None or r.submission == submission)]
    actual = populated.list(state=state, preset=preset, submission=submission)
    assert actual == expected
    assert [r.id for r in actual] == sorted(r.id for r in actual)
    assert all(isinstance(r, js.JobRecord) for r in actual)
    for record in actual:
        assert populated.get(record.id) == record


def test_empty_bound_filters_and_unknown_id(tmp_path):
    with open_store(tmp_path) as store:
        assert store.list() == []
        with pytest.raises(DownloadinuxError, match='unknown job ID 1'):
            store.get(1)
        store.submit('video', ['one'])
        assert store.list(state="queued' OR 1=1 --") == []
        assert store.list(preset="video' OR 1=1 --") == []
        assert store.list(submission=999) == []
        with pytest.raises(DownloadinuxError, match='unknown job ID 999'):
            store.get(999)
        assert len(store.list()) == 1


@pytest.mark.parametrize('invalid', [0, -1, True, False, 1.0, '1', None, [], {}])
def test_nonpositive_bool_and_wrong_id_types(tmp_path, invalid):
    with open_store(tmp_path) as store:
        with pytest.raises(DownloadinuxError, match='positive integer'):
            store.get(invalid)
        if invalid is not None:
            with pytest.raises(DownloadinuxError, match='positive integer'):
                store.list(submission=invalid)


@pytest.mark.parametrize('field', ['state', 'preset'])
@pytest.mark.parametrize('invalid', [True, 1, [], {}])
def test_filter_types_refused(tmp_path, field, invalid):
    with open_store(tmp_path) as store:
        with pytest.raises(DownloadinuxError, match='filter must be a string'):
            store.list(**{field: invalid})


def test_every_field_null_values_wait_shapes_and_independence(tmp_path):
    with open_store(tmp_path) as store:
        result = store.submit('video', ['anon'])
        expected = dict(id=result.created[0].id, submission=result.submission, preset='video', url='anon', lane=None,
                        source=None, revision=None, user_agent='', state='queued', wait=None, attempt=1,
                        cancel_requested=False, created_at='2026-10-11T12:00:00Z', queued_at='2026-10-11T12:00:00Z',
                        started_at=None, finished_at=None, exit_status=None, error=None)
        assert asdict(store.get(expected['id'])) == expected
        assert type(store.get(expected['id']).cancel_requested) is bool
        named = store.submit('gallery', ['named'], lane='gallery', source='alpha', revision='b' * 32,
                             user_agent='named UA').created[0].id
        store._connection.execute('''UPDATE jobs SET state='failed', attempt=3, cancel_requested=1,
            started_at='2026-10-11T12:01:00Z', finished_at='2026-10-11T12:02:00Z', exit_status=143,
            error='worker diagnostic' WHERE id=?''', (named,))
        filled = store.get(named)
        assert asdict(filled) == dict(expected, id=named, submission=2, preset='gallery', url='named', lane='gallery',
                                     source='alpha', revision='b' * 32, user_agent='named UA', state='failed',
                                     attempt=3, cancel_requested=True, started_at='2026-10-11T12:01:00Z',
                                     finished_at='2026-10-11T12:02:00Z', exit_status=143, error='worker diagnostic')
        for wait in ('{"reason":"vpn"}', '{"reason":"throttle","until":"2026-10-11T12:03:00Z"}'):
            store._connection.execute('UPDATE jobs SET wait=? WHERE id=1', (wait,))
            import json
            first, second = store.get(1), store.list()[0]
            assert first.wait == second.wait == json.loads(wait)
            assert first is not second and first.wait is not second.wait
            first.wait['reason'] = 'mutated'
            assert second.wait == store.get(1).wait == json.loads(wait)
        assert set(asdict(filled)) == set(js.RECORD_FIELDS)
        assert 'queue_order' not in asdict(filled)
        assert 'next_start_at' not in asdict(filled)
        saved = store.list()
    with open_store(tmp_path) as store:
        assert store.list() == saved


@pytest.mark.parametrize('value', [*(chr(i) for i in range(32)), chr(127), "'\"\\", 'é', 'e\u0301', '😀', '-UA', ''])
def test_literal_control_sweep_through_real_sqlite_and_reopen(tmp_path, value):
    url = 'literal' + value
    with open_store(tmp_path) as store:
        if value in ('\r', '\n', '\0'):
            with pytest.raises(DownloadinuxError):
                store.submit('video', [url], user_agent='okay')
            with pytest.raises(DownloadinuxError):
                store.submit('video', ['okay'], user_agent=value)
            assert contents(store) == ([], [], [(1, 0)])
            return
        job_id = store.submit('video', [url], user_agent=value).created[0].id
        assert store.get(job_id).url == url
        assert store.get(job_id).user_agent == value
    with open_store(tmp_path) as store:
        assert store.get(job_id).url == url
        assert store.get(job_id).user_agent == value


@pytest.mark.parametrize('wait', ['SENTINEL', '[]', 'null', '{"reason":"other"}',
                                 '{"reason":"vpn","extra":"SENTINEL"}',
                                 '{"reason":"throttle","until":"2026-02-30T00:00:00Z"}', '[' * 2000])
def test_invalid_stored_wait_is_safe_operation_error(tmp_path, wait):
    with open_store(tmp_path) as store:
        store.submit('video', ['one'])
        store._connection.execute('UPDATE jobs SET wait=?', (wait,))
        with pytest.raises(DownloadinuxError, match='invalid stored job wait') as error:
            store.get(1)
        assert 'SENTINEL' not in str(error.value)


def test_read_errors_are_safe(tmp_path):
    store = open_store(tmp_path)
    store.submit('video', ['one'])
    for call in (lambda: store.get(2 ** 100), lambda: store.list(submission=2 ** 100),
                 lambda: store.list(state='SENTINEL\ud800')):
        with pytest.raises(DownloadinuxError, match='jobs.sqlite3') as error:
            call()
        assert 'SENTINEL' not in str(error.value)
    store.close()
    with pytest.raises(DownloadinuxError, match='jobs.sqlite3'):
        store.list()


def test_isolated_stdlib_only_open_submit_read_without_host_roots(tmp_path):
    root = tmp_path / 'isolated-state'
    root.mkdir()
    script = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split('.')[0]
        if top not in sys.stdlib_module_names and top != 'downloadinux':
            raise ImportError('non-stdlib import: ' + name)
        if top == 'downloadinux' and name not in ('downloadinux', 'downloadinux.job_store', 'downloadinux.errors',
                                                  'downloadinux.cookie_store', 'downloadinux.cookiejar'):
            raise ImportError('host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import job_store as js
root = Path(sys.argv[2])
with js.open_store(root) as store:
    a = store.submit('video', ['anon'], user_agent='')
    b = store.submit('gallery', ['named'], source='alpha', revision='a' * 32, user_agent='-literal')
    assert store.get(a.created[0].id).source is None
    assert store.get(b.created[0].id).revision == 'a' * 32
    assert len(store.list()) == 2
with js.open_store(root) as store:
    assert [j.url for j in store.list()] == ['anon', 'named']
assert sorted(p.name for p in root.iterdir()) == ['jobs']
print('stdlib boundary passed')
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(SRC), str(root)],
                            capture_output=True, text=True, timeout=10,
                            env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path / 'absent-home')})
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'stdlib boundary passed\n'


# Store lifecycle (§5 "Store lifecycle", DEC-46).

def sql(store, statement, *parameters):
    return store._connection.execute(statement, parameters).fetchall()


def set_lane_deadline(store, preset, value):
    sql(store, 'UPDATE lanes SET next_start_at=? WHERE preset=?', value, preset)


@pytest.mark.parametrize('until', ['٢٠٢٦-10-11T12:03:00Z', '２０２６-10-11T12:03:00Z', '2026-10-11T12:0٣:00Z'])
def test_non_ascii_digit_stored_until_refuses(tmp_path, until):
    with open_store(tmp_path) as store:
        store.submit('video', ['one'], lane='video')
        import json
        sql(store, 'UPDATE jobs SET wait=?', json.dumps({'reason': 'throttle', 'until': until}))
        with pytest.raises(DownloadinuxError, match='invalid stored job wait'):
            store.get(1)


@pytest.mark.parametrize('source,revision', [('alpha٣', 'a' * 32), ('ａlpha', 'a' * 32), ('alpha', 'a' * 31 + '٣'),
                                             ('alpha', 'a' * 31 + '３'), ('alpha\n', 'a' * 32)])
def test_unicode_lookalike_identifiers_refuse(tmp_path, source, revision):
    with open_store(tmp_path) as store:
        with pytest.raises(DownloadinuxError, match='cookie selection'):
            store.submit('video', ['one'], source=source, revision=revision)
        assert contents(store) == ([], [], [(1, 0)])


def test_job_store_uses_the_shared_identifier_patterns():
    assert js.cookie_store.TIMESTAMP.flags & __import__('re').ASCII
    source = (SRC / 'downloadinux' / 'job_store.py').read_text()
    assert '\\d' not in source and 'import re' not in source


def queue_fixture(store):
    """Two lanes, three immediate submissions, a retried shape, mixed states."""
    v = store.submit('video', ['v1', 'v2', 'v3'], lane='video').created
    g = store.submit('gallery', ['g1', 'g2'], lane='gallery').created
    i1 = store.submit('video', ['i1a', 'i1b']).created
    i2 = store.submit('gallery', ['i2a']).created
    i3 = store.submit('video', ['i3a']).created
    # Video lane: v1 running; v3's order precedes v2's although queued_at runs backward.
    sql(store, "UPDATE jobs SET state='running' WHERE id=?", v[0].id)
    sql(store, "UPDATE jobs SET queue_order=100, queued_at='2026-10-11T11:00:00Z' WHERE id=?", v[1].id)
    sql(store, "UPDATE jobs SET queue_order=50, queued_at='2026-10-11T13:00:00Z' WHERE id=?", v[2].id)
    # Gallery lane: all queued, both with the same queued_at.
    # i1: head running is absent; i1a finished, i1b queued.
    sql(store, "UPDATE jobs SET state='succeeded' WHERE id=?", i1[0].id)
    # i2: only finished jobs, so absent.
    sql(store, "UPDATE jobs SET state='failed' WHERE id=?", i2[0].id)
    # i3: its only job running, so absent.
    sql(store, "UPDATE jobs SET state='running' WHERE id=?", i3[0].id)
    # A retried immediate job formed a new submission 6, built by SQL.
    sql(store, "INSERT INTO submissions(created_at) VALUES ('2026-10-11T12:00:00Z')")
    sql(store, "UPDATE jobs SET submission=6, queue_order=200, attempt=2 WHERE id=?", i1[0].id)
    sql(store, "UPDATE jobs SET state='queued' WHERE id=?", i1[0].id)
    return v, g, i1, i2, i3


def test_queue_heads_order_running_and_deadlines(tmp_path):
    with open_store(tmp_path) as store:
        v, g, i1, i2, i3 = queue_fixture(store)
        set_lane_deadline(store, 'video', '2026-10-11T12:05:00Z')
        heads = store.queue_heads()
        assert [(h.lane, h.submission, h.head.id, h.running, h.next_start_at) for h in heads] == [
            ('gallery', None, g[0].id, False, None),
            ('video', None, v[2].id, True, '2026-10-11T12:05:00Z'),
            (None, 3, i1[1].id, False, None),
            (None, 6, i1[0].id, False, None),
        ]
        assert all(isinstance(h.head, js.JobRecord) for h in heads)
        assert heads[1].head == store.get(v[2].id)
        assert 'queue_order' not in asdict(heads[0].head)
        # No queued jobs anywhere: no entries.
        sql(store, "UPDATE jobs SET state='cancelled' WHERE state='queued'")
        assert store.queue_heads() == ()


def test_queue_heads_lane_with_only_running_absent(tmp_path):
    with open_store(tmp_path) as store:
        store.submit('video', ['one'], lane='video')
        sql(store, "UPDATE jobs SET state='running'")
        set_lane_deadline(store, 'video', '2026-10-11T12:05:00Z')
        assert store.queue_heads() == ()


def test_invalid_stored_lane_deadline_is_safe(tmp_path):
    with open_store(tmp_path) as store:
        store.submit('video', ['one'], lane='video')
        set_lane_deadline(store, 'video', 'SENTINEL٢')
        with pytest.raises(DownloadinuxError, match='invalid stored lane deadline') as error:
            store.queue_heads()
        assert 'SENTINEL' not in str(error.value)
        with pytest.raises(DownloadinuxError, match='invalid stored lane deadline'):
            store.start(1)
        assert store.get(1).state == 'queued'


def test_start_success_fields(tmp_path):
    clock = [NOW]
    with open_store(tmp_path, clock=lambda: clock[0]) as store:
        job = store.submit('video', ['one'], lane='video').created[0].id
        store.set_wait(job, 'vpn')
        clock[0] = datetime(2026, 10, 11, 12, 1, 30, 999, tzinfo=timezone.utc)
        before = store.get(job)
        started = store.start(job)
        assert started == store.get(job)
        assert asdict(started) == dict(asdict(before), state='running', started_at='2026-10-11T12:01:30Z',
                                       wait=None)
    with open_store(tmp_path) as store:
        assert store.get(job) == started


def snapshot(store):
    return tuple(sql(store, f'SELECT * FROM {table}') for table in
                 ('submissions', 'jobs', 'lanes', 'queue_counter'))


@pytest.mark.parametrize('case', ['running', 'succeeded', 'failed', 'cancelled', 'interrupted',
                                  'not-head', 'queue-busy', 'deadline-after'])
def test_start_refusals_change_nothing(tmp_path, case):
    with open_store(tmp_path) as store:
        first, second = (r.id for r in store.submit('video', ['a', 'b'], lane='video').created)
        target = first
        if case in js.STATES:
            sql(store, 'UPDATE jobs SET state=? WHERE id=?', case, first)
        elif case == 'not-head':
            target = second
        elif case == 'queue-busy':
            sql(store, "UPDATE jobs SET state='running' WHERE id=?", first)
            target = second
        elif case == 'deadline-after':
            set_lane_deadline(store, 'video', '2026-10-11T12:00:01Z')
        before = snapshot(store)
        message = {'not-head': f'job {second} does not head its queue; job {first} does',
                   'queue-busy': f'job {second} cannot start while job {first} runs',
                   'deadline-after': 'before its lane deadline 2026-10-11T12:00:01Z'}.get(case, f'job {first} is {case}')
        with pytest.raises(DownloadinuxError, match=message):
            store.start(target)
        assert snapshot(store) == before
        assert not store._connection.in_transaction


def test_start_deadline_equal_to_now_allowed_and_immediate_ignores_deadline(tmp_path):
    with open_store(tmp_path) as store:
        lane = store.submit('video', ['a'], lane='video').created[0].id
        immediate = store.submit('video', ['b']).created[0].id
        set_lane_deadline(store, 'video', '2026-10-11T12:00:00Z')
        assert store.start(lane).state == 'running'
        set_lane_deadline(store, 'video', '2030-01-01T00:00:00Z')
        assert store.start(immediate).state == 'running'


def test_start_lane_and_immediate_queues_are_independent(tmp_path):
    with open_store(tmp_path) as store:
        lane = [r.id for r in store.submit('video', ['l1', 'l2'], lane='video').created]
        imm = [r.id for r in store.submit('video', ['i1', 'i2']).created]
        other = store.submit('video', ['o1']).created[0].id
        store.start(lane[0])
        assert store.start(imm[0]).state == 'running'   # lane busy does not block immediate
        assert store.start(other).state == 'running'    # one immediate busy does not block another
        with pytest.raises(DownloadinuxError, match='runs in its queue'):
            store.start(imm[1])
        with pytest.raises(DownloadinuxError, match='runs in its queue'):
            store.start(lane[1])
        sql(store, "UPDATE jobs SET state='succeeded' WHERE id=?", lane[0])
        assert store.start(lane[1]).state == 'running'  # immediate busy does not block the lane


@pytest.mark.parametrize('invalid', [0, -1, True, 1.0, '1', None])
def test_start_and_wait_id_validation(tmp_path, invalid):
    with open_store(tmp_path) as store:
        for call in (lambda: store.start(invalid), lambda: store.set_wait(invalid, 'vpn')):
            with pytest.raises(DownloadinuxError, match='positive integer'):
                call()
        for call in (lambda: store.start(5), lambda: store.set_wait(5, None)):
            with pytest.raises(DownloadinuxError, match='unknown job ID 5'):
                call()


def test_waits_set_copy_clear_and_reopen(tmp_path):
    with open_store(tmp_path) as store:
        job = store.submit('gallery', ['a'], lane='gallery').created[0].id
        assert store.set_wait(job, 'vpn').wait == {'reason': 'vpn'}
        set_lane_deadline(store, 'gallery', '2026-10-11T12:04:10Z')
        assert store.set_wait(job, 'throttle').wait == {'reason': 'throttle', 'until': '2026-10-11T12:04:10Z'}
    with open_store(tmp_path) as store:
        assert store.get(job).wait == {'reason': 'throttle', 'until': '2026-10-11T12:04:10Z'}
        assert store.set_wait(job, None).wait is None
        assert store.get(job).wait is None


@pytest.mark.parametrize('case', ['running', 'succeeded', 'cancelled', 'immediate', 'no-deadline', 'reason'])
def test_wait_refusals_change_nothing(tmp_path, case):
    with open_store(tmp_path) as store:
        job = store.submit('video', ['a'], lane=None if case == 'immediate' else 'video').created[0].id
        set_lane_deadline(store, 'video', None if case == 'no-deadline' else '2026-10-11T12:04:10Z')
        reason = 'throttle'
        if case in js.STATES:
            sql(store, 'UPDATE jobs SET state=? WHERE id=?', case, job)
        before = snapshot(store)
        expected = {'immediate': 'is immediate', 'no-deadline': 'lane has no deadline',
                    'reason': 'wait reason must be'}.get(case, f'job {job} is {case}')
        for reason in (('Throttle', 'other', True, 1) if case == 'reason' else
                       ('throttle', 'vpn', None) if case in js.STATES else ('throttle',)):
            with pytest.raises(DownloadinuxError, match=expected):
                store.set_wait(job, reason)
        assert snapshot(store) == before
