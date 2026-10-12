"""The worker's runner execution library (DEC-48).

Target design, *Persistent worker and jobs* §5, "Runner execution, cancel
signals, and the shutdown wait", driven through the real job store, the real
scheduler, and real child processes: a fixture `downloadinux-runner` first on
`PATH` records what it received and then follows the mode its URL spells
(comma-separated words), so no test fakes a process or its failure.
"""
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

import pytest

from downloadinux import cookie_runner, cookie_store, job_store as js, launch, worker_execution as we
from downloadinux import worker_scheduler as ws
from downloadinux.errors import DownloadinuxError

from snapshot_support import open_fds, wait_for

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
UA = 'Mozilla/5.0 synthetic'
ALPHA = '1' * 32

# The fixture runner writes `<pid>.json` once it has recorded what it
# received, then follows its URL's words:
#   output       write interleaved stdout/stderr bytes, invalid UTF-8 included
#   wait=NAME    wait until FIXTURE_LOG/release-NAME (or release-all) exists
#   term=die     on SIGTERM, record it and die by it (the default)
#   term=ignore  record SIGTERM and keep going
#   term=exitN   record SIGTERM and exit N
#   kill=N       die by signal N
#   exit=N       exit N (default 0)
FIXTURE = r'''#!{python} -I
import json, os, signal, stat, sys, time
log = os.environ['FIXTURE_LOG']
url = sys.argv[-1]
words = dict(word.partition('=')[::2] for word in url.split(',') if word)
base = os.path.join(log, str(os.getpid()))

def on_term(number, frame):
    with open(base + '.signals', 'a') as handle:
        handle.write('%d\n' % number)
    action = words.get('term', 'die')
    if action == 'ignore':
        return
    if action.startswith('exit'):
        os._exit(int(action[4:]))
    signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)
signal.signal(signal.SIGTERM, on_term)

fds = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
    except OSError:
        continue
    fds.append(fd)
null, stdin = os.stat('/dev/null'), os.fstat(0)
record = dict(argv=sys.argv, pid=os.getpid(), pgid=os.getpgid(0), cwd=os.getcwd(), fds=fds,
              stdin_null=(stdin.st_rdev, stat.S_IFMT(stdin.st_mode)) == (null.st_rdev, stat.S_IFMT(null.st_mode)),
              stdin_eof=os.read(0, 1) == b'', mark=os.environ.get('FIXTURE_MARK'), url=url)
with open(base + '.tmp', 'w') as handle:
    json.dump(record, handle)
os.rename(base + '.tmp', base + '.json')

if 'output' in words:
    for fd, chunk in ((1, b'out-1\n'), (2, b'err-1 \xff\xfe\n'), (1, b'out-2 \x80'), (2, b'err-2\n')):
        os.write(fd, chunk)
if 'wait' in words:
    release = os.path.join(log, 'release-' + words['wait'])
    everything = os.path.join(log, 'release-all')
    while not (os.path.exists(release) or os.path.exists(everything)):
        time.sleep(0.01)
if 'kill' in words:
    number = int(words['kill'])
    if number != signal.SIGKILL:
        signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)
sys.exit(int(words.get('exit', 0)))
'''


class Wall:
    """A mutable aware UTC wall clock shared by the store and the scheduler."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Rig:
    """A state root with an owned store, a fixture runner on `PATH`, and roots."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.state = tmp_path / 'state'
        self.cookies = tmp_path / 'cookies'
        self.downloads = tmp_path / 'downloads'
        self.log = tmp_path / 'fixture-log'
        self.bin = tmp_path / 'bin'
        for directory in (self.state, self.cookies, self.downloads, self.log, self.bin):
            directory.mkdir()
        self.runner = self.bin / we.RUNNER
        self.runner.write_text(FIXTURE.replace('{python}', sys.executable))
        self.runner.chmod(0o755)
        monkeypatch.setenv('PATH', str(self.bin))
        monkeypatch.setenv('FIXTURE_LOG', str(self.log))
        monkeypatch.setenv('FIXTURE_MARK', 'inherited')
        self.wall = Wall()
        self.executors = []
        self.stores = []

    def open(self, **kwargs):
        kwargs.setdefault('clock', self.wall)
        store = js.open_store(self.state, **kwargs)
        assert isinstance(store, js.JobStore)
        self.stores.append(store)
        return store

    def executor(self, store, **kwargs):
        kwargs.setdefault('cookies', self.cookies)
        kwargs.setdefault('state', self.state)
        kwargs.setdefault('downloads', self.downloads)
        executor = we.Executor(store, **kwargs)
        self.executors.append(executor)
        return executor

    def scheduler(self, store, executor, **kwargs):
        return ws.Scheduler(store, executor.launch, vpn=False, clock=self.wall, **kwargs)

    def records(self):
        return [json.loads(path.read_text()) for path in sorted(self.log.glob('*.json'))]

    def record(self, url):
        wait_for(lambda: any(r['url'] == url for r in self.records()))
        [record] = [r for r in self.records() if r['url'] == url]
        return record

    def signals(self, record):
        path = self.log / f'{record["pid"]}.signals'
        return [int(line) for line in path.read_text().split()] if path.exists() else []

    def release(self, name='all'):
        (self.log / f'release-{name}').touch()

    def close(self):
        """Release every waiting fixture, kill any that ignore it, and close stores."""
        self.release()
        for executor in self.executors:
            for process in executor._running.values():
                try:
                    process.wait(10)
                except Exception:
                    process.kill()
                    process.wait()
        for store in self.stores:
            store.close()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    yield rig
    rig.close()


def poll_until(executor, count, timeout=30):
    """Poll until `count` jobs have ended; returns the ended records in poll order."""
    ended = []

    def done():
        ended.extend(executor.poll())
        return len(ended) >= count
    wait_for(done, timeout)
    return ended


def log_path(rig, job_id, attempt=1):
    return rig.state / 'jobs' / 'logs' / f'{job_id}-{attempt}.log'


def deadline(store, preset):
    return store._connection.execute('SELECT next_start_at FROM lanes WHERE preset=?', (preset,)).fetchone()[0]


# Store helpers (§2).

def test_log_name_and_logs_directory(rig):
    assert js.log_name(7, 1) == '7-1.log'
    assert js.log_name(12, 30) == '12-30.log'
    for job_id, attempt in ((0, 1), (1, 0), (-1, 1), (True, 1), (1, True), (1.0, 1), ('1', 1), (1, None)):
        with pytest.raises(DownloadinuxError, match='^job ID and attempt must be positive integers$'):
            js.log_name(job_id, attempt)
    store = rig.open()
    assert store.logs == rig.state / 'jobs' / 'logs'
    assert store.logs.is_dir() and stat.S_IMODE(store.logs.stat().st_mode) == 0o700


# Roots and argument list (§5, DEC-48).

@pytest.mark.parametrize('preset', ['video', 'gallery'])
@pytest.mark.parametrize('named', [True, False])
@pytest.mark.parametrize('user_agent', ['', UA, '-', '--anonymous', '-leading dash', ' spaced '])
@pytest.mark.parametrize('url', ['https://example.com/a?b=1&c=2', '-dash-url', '--source'])
def test_argv_matches_the_host_list_and_parses_as_the_runner(rig, preset, named, user_agent, url):
    store = rig.open()
    pair = dict(source='alpha', revision=ALPHA) if named else {}
    job = store.submit(preset, [url], user_agent=user_agent, **pair).created[0].id
    record = store.get(job)
    argv = we.runner_argv(record, Path('/cookies'), Path('/state'))
    selection = cookie_store.Pinned('alpha', ALPHA) if named else cookie_store.ANONYMOUS
    assert argv == launch.runner_argv('download', selection, user_agent, preset, [url])
    request = cookie_runner.parse(argv[1:])
    assert (request.command, request.cookies, request.state) == ('download', Path('/cookies'), Path('/state'))
    assert request.pinned == selection
    assert (request.user_agent, request.preset.name, request.operands) == (user_agent, preset, [url])


# Launch and logs through the real scheduler (§5 *Attempt log*, *Launch*).

def test_a_pass_launches_every_queue_head_with_its_private_log(rig):
    store = rig.open()
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    video = store.submit('video', ['v1,output,wait=a', 'v2'], lane='video', source='alpha', revision=ALPHA,
                         user_agent='-ua').created
    gallery = store.submit('gallery', ['g1,wait=a'], lane='gallery', user_agent='').created
    now = store.submit('video', ['i1,wait=a', 'i2'], user_agent=UA).created
    before = open_fds()
    started = run.run_pass()
    assert started == (gallery[0].id, video[0].id, now[0].id)
    assert executor.running == tuple(sorted(started))
    assert open_fds() == before  # the parent's log copies are closed
    for job in (video[0], gallery[0], now[0]):
        record = rig.record(job.url)
        assert record['argv'][1:] == we.runner_argv(store.get(job.id), rig.cookies, rig.state)[1:]
        assert record['argv'][0] == str(rig.runner)  # resolved through PATH
        assert record['cwd'] == str(rig.downloads.resolve())
        assert record['pgid'] == record['pid'] != os.getpgid(0)
        assert record['stdin_null'] and record['stdin_eof']
        assert record['fds'] == []
        assert record['mark'] == 'inherited'  # the environment is inherited unchanged
    rig.release('a')
    ended = poll_until(executor, 3)
    assert [r.id for r in ended] == sorted(started)
    assert all(r.state == 'succeeded' and r.exit_status == 0 for r in ended)
    assert log_path(rig, video[0].id).read_bytes() == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    for job in (gallery[0], now[0]):
        assert log_path(rig, job.id).read_bytes() == b''  # the worker writes nothing
    assert stat.S_IMODE(log_path(rig, video[0].id).stat().st_mode) == 0o600
    assert executor.running == ()
    # The queue's next job runs in a later pass, into its own log.
    assert run.run_pass() == (now[1].id,)
    poll_until(executor, 1)
    assert log_path(rig, now[1].id).exists() and not log_path(rig, video[1].id).exists()


@pytest.mark.parametrize('umask', [0, 0o077, 0o277, 0o022])
def test_a_new_log_is_0600_under_any_umask(rig, umask):
    store = rig.open()
    executor = rig.executor(store)
    job = store.submit('video', ['output']).created[0].id
    previous = os.umask(umask)
    try:
        executor.launch(store.start(job))
    finally:
        os.umask(previous)
    poll_until(executor, 1)
    assert stat.S_IMODE(log_path(rig, job).stat().st_mode) == 0o600
    assert log_path(rig, job).read_bytes().startswith(b'out-1\n')


def test_an_existing_log_is_appended_and_made_private(rig):
    store = rig.open()
    executor = rig.executor(store)
    job = store.submit('video', ['output']).created[0].id
    log = log_path(rig, job)
    log.write_bytes(b'earlier bytes\n')
    log.chmod(0o644)
    executor.launch(store.start(job))
    poll_until(executor, 1)
    assert log.read_bytes() == b'earlier bytes\nout-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


# Launch failures (§5 *Launch*; §3's "cannot be started" row; DEC-47's handoff).

def fail_through_a_pass(rig, executor, store, expected):
    """Lane and immediate queues all hit the same failure in one pass."""
    run = rig.scheduler(store, executor)
    video = store.submit('video', ['v1', 'v2'], lane='video').created
    now = store.submit('gallery', ['i1', 'i2']).created
    before = open_fds()
    assert run.run_pass() == ()
    assert open_fds() == before
    for job in (video[0], now[0]):
        record = store.get(job.id)
        assert (record.state, record.error, record.exit_status) == ('failed', expected(job.id), None)
        assert record.finished_at == NOW.strftime(js.TIMESTAMP_FORMAT)
    assert deadline(store, 'video') is not None and deadline(store, 'gallery') is None
    assert [store.get(j.id).state for j in (video[1], now[1])] == ['queued', 'queued']
    assert executor.running == ()
    return video, now


def test_an_absent_runner_fails_the_job(rig, monkeypatch):
    empty = rig.tmp / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    store = rig.open()
    fail_through_a_pass(rig, rig.executor(store), store,
                        lambda _: f'downloadinux-runner: {os.strerror(errno.ENOENT)}')


def test_a_non_executable_runner_fails_the_job(rig):
    rig.runner.chmod(0o644)
    store = rig.open()
    fail_through_a_pass(rig, rig.executor(store), store,
                        lambda _: f'downloadinux-runner: {os.strerror(errno.EACCES)}')


def test_a_missing_download_root_fails_the_job_naming_it(rig):
    store = rig.open()
    missing = rig.tmp / 'no-downloads'
    fail_through_a_pass(rig, rig.executor(store, downloads=missing), store,
                        lambda _: f'{missing}: {os.strerror(errno.ENOENT)}')


def test_a_start_failure_that_is_not_an_os_error_is_launch_failed(rig):
    store = rig.open()
    fail_through_a_pass(rig, rig.executor(store, cookies='/coo\0kies'), store, lambda _: 'launch failed')


def test_an_os_failure_without_a_reason_is_launch_failed(rig, monkeypatch):
    # No real start failure lacks strerror; a bare OSError is that shape.
    def refuse(*args, **kwargs):
        raise OSError()
    monkeypatch.setattr(we.subprocess, 'Popen', refuse)
    store = rig.open()
    fail_through_a_pass(rig, rig.executor(store), store, lambda _: 'launch failed')


def plant_directory(path):
    path.mkdir()


def plant_symlink(path):
    path.symlink_to(path.parent / 'elsewhere')


def plant_dangling_symlink_to_fifo(path):
    os.mkfifo(path.parent / 'fifo-target')
    path.symlink_to(path.parent / 'fifo-target')


def plant_fifo(path):
    os.mkfifo(path)


@pytest.mark.parametrize('plant,code', [
    (plant_directory, errno.EISDIR),
    (plant_symlink, errno.ELOOP),
    (plant_dangling_symlink_to_fifo, errno.ELOOP),
    (plant_fifo, errno.ENXIO),  # no reader: refused at once, never a stall
])
def test_a_planted_log_fails_only_its_job(rig, plant, code):
    store = rig.open(delay=lambda: 0)
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    video = store.submit('video', ['v1', 'v2'], lane='video').created
    gallery = store.submit('gallery', ['g1'], lane='gallery').created
    now = store.submit('video', ['i1']).created
    plant(log_path(rig, video[0].id))
    started = time.monotonic()
    assert run.run_pass() == (gallery[0].id, now[0].id)
    assert time.monotonic() - started < 10
    failed = store.get(video[0].id)
    assert (failed.state, failed.exit_status) == ('failed', None)
    assert failed.error == f'{log_path(rig, video[0].id)}: {os.strerror(code)}'
    assert deadline(store, 'video') == NOW.strftime(js.TIMESTAMP_FORMAT)
    assert store.get(video[1].id).state == 'queued'
    poll_until(executor, 2)


def test_a_fifo_with_a_reader_is_not_a_regular_file(rig):
    store = rig.open()
    executor = rig.executor(store)
    job = store.submit('video', ['v1']).created[0].id
    path = log_path(rig, job)
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        record = store.start(job)
        with pytest.raises(DownloadinuxError) as error:
            executor.launch(record)
        assert str(error.value) == f'{path}: not a regular file'
    finally:
        os.close(reader)
    assert executor.running == () and not rig.records()


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_an_unwritable_logs_directory_fails_the_job(rig):
    store = rig.open()
    store.logs.chmod(0o500)
    try:
        fail_through_a_pass(rig, rig.executor(store), store,
                            lambda job: f'{log_path(rig, job)}: {os.strerror(errno.EACCES)}')
    finally:
        store.logs.chmod(0o700)


def test_a_symlinked_logs_directory_fails_the_job(rig):
    store = rig.open()
    elsewhere = rig.tmp / 'elsewhere-logs'
    store.logs.rename(elsewhere)
    store.logs.symlink_to(elsewhere)
    # The platform's own refusal: ENOTDIR on macOS, ELOOP on Linux.
    with pytest.raises(OSError) as refusal:
        os.open(store.logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    assert refusal.value.errno in (errno.ENOTDIR, errno.ELOOP)
    fail_through_a_pass(rig, rig.executor(store), store,
                        lambda job: f'{log_path(rig, job)}: {refusal.value.strerror}')
    assert list(elsewhere.iterdir()) == []


def test_a_failed_lane_launch_keeps_its_log_and_the_next_job_waits_for_a_later_pass(rig):
    store = rig.open(delay=lambda: 0)
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    video = store.submit('video', ['v1', 'v2'], lane='video').created
    rig.runner.chmod(0o644)
    assert run.run_pass() == ()
    assert log_path(rig, video[0].id).read_bytes() == b''  # opened, kept, empty
    rig.runner.chmod(0o755)
    assert run.run_pass() == (video[1].id,)
    poll_until(executor, 1)


# Exits (§5 *Exits*; §3's rows).

@pytest.mark.parametrize('url,state,status', [
    ('exit=0', 'succeeded', 0),
    ('exit=1', 'failed', 1),
    ('exit=255', 'failed', 255),
    ('kill=15', 'failed', 143),
    ('kill=2', 'failed', 130),
    ('kill=1', 'failed', 129),
    ('kill=9', 'failed', 137),
])
@pytest.mark.parametrize('lane', [True, False])
def test_each_exit_status_ends_the_job_through_the_store(rig, url, state, status, lane):
    store = rig.open(delay=lambda: 7)
    executor = rig.executor(store)
    job = store.submit('gallery', [url], lane='gallery' if lane else None).created[0].id
    assert rig.scheduler(store, executor).run_pass() == (job,)
    rig.wall.advance(5)
    [ended] = poll_until(executor, 1)
    assert ended == store.get(job)
    assert (ended.state, ended.exit_status, ended.error) == (state, status, None)
    assert ended.finished_at == '2026-10-12T12:00:05Z'
    assert deadline(store, 'gallery') == ('2026-10-12T12:00:12Z' if lane else None)
    assert executor.running == ()


def test_polls_end_in_job_id_order_and_never_block(rig):
    store = rig.open()
    executor = rig.executor(store)
    jobs = [store.submit('video', [f'exit={n},wait=w{n}']).created[0].id for n in (3, 4, 5)]
    for job in jobs:
        executor.launch(store.start(job))
    for job in jobs:
        rig.record(store.get(job).url)
    before = [store.get(job) for job in jobs]
    started = time.monotonic()
    assert executor.poll() == ()
    assert time.monotonic() - started < 1
    assert [store.get(job) for job in jobs] == before
    rig.release('w5')
    rig.release('w3')
    # Both have exited before the one poll that ends them.
    wait_for(lambda: all(executor._running[job].poll() is not None for job in (jobs[0], jobs[2])))
    ended = executor.poll()
    assert [(r.id, r.exit_status) for r in ended] == [(jobs[0], 3), (jobs[2], 5)]
    assert executor.running == (jobs[1],)
    rig.release('w4')
    assert [r.id for r in poll_until(executor, 1)] == [jobs[1]]


def test_a_failed_end_propagates_keeps_the_runner_and_a_later_poll_ends_it(rig):
    draws = [-1, 9]
    store = rig.open(delay=lambda: draws.pop(0))
    executor = rig.executor(store)
    job = store.submit('video', ['exit=4'], lane='video').created[0].id
    executor.launch(store.start(job))
    with pytest.raises(DownloadinuxError, match='delay source must return a nonnegative integer'):
        wait_for(lambda: executor.poll() and False)
    assert executor.running == (job,)
    assert store.get(job).state == 'running'
    [ended] = executor.poll()
    assert (ended.id, ended.state, ended.exit_status) == (job, 'failed', 4)
    assert deadline(store, 'video') == '2026-10-12T12:00:09Z'
    assert executor.running == ()


def test_a_closed_store_fails_the_poll_and_keeps_the_runner(rig):
    store = rig.open()
    executor = rig.executor(store)
    job = store.submit('video', ['exit=0']).created[0].id
    executor.launch(store.start(job))
    rig.record('exit=0')
    store.close()
    with pytest.raises(DownloadinuxError):
        wait_for(lambda: executor.poll() and False)
    assert executor.running == (job,)
