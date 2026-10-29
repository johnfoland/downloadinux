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

from downloadinux import cookie_runner, cookie_snapshot, cookie_store, job_store as js, launch
from downloadinux import worker_execution as we
from downloadinux import worker_scheduler as ws
from downloadinux.errors import DownloadinuxError

from runner_support import STUB
from snapshot_support import SRC, open_fds, roots, wait_for

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
UA = 'Mozilla/5.0 synthetic'
ALPHA = '1' * 32

# The fixture runner writes `<pid>.json` once it has recorded what it
# received, then follows its URL's words:
#   output       write interleaved stdout/stderr bytes, invalid UTF-8 included
#   wait=NAME    wait until FIXTURE_LOG/release-NAME (or release-all) exists
#   term=die     on SIGTERM, record it and die by it (the default)
#   term=ignore  record SIGTERM and keep going
#   term=ignoreN record SIGTERM; keep going for the first N, then die by it
#   term=exitN   record SIGTERM and exit N
#   kill=N       die by signal N
#   exit=N       exit N (default 0)
FIXTURE = r'''#!{python} -I
import json, os, signal, stat, sys, time
log = os.environ['FIXTURE_LOG']
url = sys.argv[-1]
words = dict(word.partition('=')[::2] for word in url.split(',') if word)
base = os.path.join(log, str(os.getpid()))

received = []

def on_term(number, frame):
    with open(base + '.signals', 'a') as handle:
        handle.write('%d\n' % number)
    received.append(number)
    action = words.get('term', 'die')
    if action == 'ignore' or (action.startswith('ignore') and len(received) <= int(action[6:])):
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


def exited(pid):
    """Whether child `pid` has exited, leaving it for the executor to reap."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


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
    # One poll after all three have exited, so it ends them together, in job
    # ID order; successive polls would return them in exit order.
    pids = [rig.record(job.url)['pid'] for job in (video[0], gallery[0], now[0])]
    wait_for(lambda: all(exited(pid) for pid in pids))
    ended = executor.poll()
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


# Cancel signals (§5 *Cancel signals*).

def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def started(rig, store, executor, url, **submit):
    job = store.submit('video', [url], **submit).created[0].id
    executor.launch(store.start(job))
    return job, rig.record(url)


def test_cancel_signals_a_running_runner_which_ends_cancelled(rig):
    store = rig.open()
    executor = rig.executor(store)
    job, record = started(rig, store, executor, 'wait=x')
    outcomes = executor.cancel([job])
    assert outcomes == (js.CancelOutcome(job, js.SIGNAL, 'running'),)
    [ended] = poll_until(executor, 1)
    assert (ended.state, ended.exit_status, ended.cancel_requested) == ('cancelled', 143, True)
    assert rig.signals(record) == [signal.SIGTERM]


def test_a_runner_exiting_0_after_cancel_succeeded(rig):
    store = rig.open()
    executor = rig.executor(store)
    job, record = started(rig, store, executor, 'wait=x,term=exit0')
    executor.cancel([job])
    [ended] = poll_until(executor, 1)
    assert (ended.state, ended.exit_status, ended.cancel_requested) == ('succeeded', 0, True)
    assert rig.signals(record) == [signal.SIGTERM]


def test_cancelling_again_signals_again(rig):
    store = rig.open()
    executor = rig.executor(store)
    job, record = started(rig, store, executor, 'wait=x,term=ignore,exit=3')
    executor.cancel([job])
    wait_for(lambda: rig.signals(record) == [signal.SIGTERM])
    assert executor.cancel([job, job]) == (js.CancelOutcome(job, js.SIGNAL, 'running'),)
    wait_for(lambda: rig.signals(record) == [signal.SIGTERM] * 2)
    assert executor.poll() == ()
    rig.release('x')
    [ended] = poll_until(executor, 1)
    assert (ended.state, ended.exit_status) == ('cancelled', 3)
    assert rig.signals(record) == [signal.SIGTERM] * 2


class Kills:
    """Record every os.kill while passing it to the real call."""

    def __init__(self, monkeypatch):
        self.calls = []
        real = os.kill

        def kill(pid, number):
            self.calls.append((pid, number))
            return real(pid, number)
        monkeypatch.setattr(os, 'kill', kill)


def test_cancel_outcomes_pass_through_and_only_registered_live_runners_are_signalled(rig, monkeypatch):
    store = rig.open(delay=lambda: 0)
    executor = rig.executor(store)
    lane = store.submit('video', ['wait=x,exit=0', 'queued'], lane='video').created
    executor.launch(store.start(lane[0].id))
    running = rig.record('wait=x,exit=0')
    finished, _ = started(rig, store, executor, 'exit=0')
    poll_until(executor, 1)
    exited, gone = started(rig, store, executor, 'exit=5')
    # Exited but not yet reaped or polled: waitid peeks without reaping.
    wait_for(lambda: os.waitid(os.P_PID, gone['pid'], os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
    unlaunched = store.submit('gallery', ['unlaunched']).created[0].id
    store.start(unlaunched)  # committed, never launched: no registered runner
    kills = Kills(monkeypatch)
    outcomes = executor.cancel([999, finished, lane[1].id, lane[0].id, exited, unlaunched])
    assert outcomes == (
        js.CancelOutcome(999, js.UNKNOWN, None),
        js.CancelOutcome(finished, js.REFUSED, 'succeeded'),
        js.CancelOutcome(lane[1].id, js.CANCELLED, 'cancelled'),
        js.CancelOutcome(lane[0].id, js.SIGNAL, 'running'),
        js.CancelOutcome(exited, js.SIGNAL, 'running'),
        js.CancelOutcome(unlaunched, js.SIGNAL, 'running'),
    )
    assert kills.calls == [(running['pid'], signal.SIGTERM)]  # a process, never a group
    assert deadline(store, 'video') is None  # a queued cancel draws no throttle
    assert store.get(lane[1].id).finished_at is not None
    ended = {r.id: r for r in poll_until(executor, 2)}
    assert (ended[lane[0].id].state, ended[lane[0].id].exit_status) == ('cancelled', 143)
    assert (ended[exited].state, ended[exited].exit_status) == ('cancelled', 5)
    assert store.get(unlaunched).state == 'running'
    assert executor.shutdown(rig.scheduler(store, executor)) == (unlaunched,)
    assert (store.get(unlaunched).state, store.get(unlaunched).error) == ('interrupted', 'worker stopped')


class Foreign:
    """A registered runner whose PID belongs to another user: signalling it
    makes the real kill(2) fail with EPERM."""

    returncode = None
    pid = 1

    def poll(self):
        return None

    def send_signal(self, number):
        os.kill(self.pid, number)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason='root may signal any process')
def test_a_signal_error_is_raised_after_every_signal_and_the_commit(rig):
    store = rig.open()
    executor = rig.executor(store)
    foreign = store.submit('video', ['foreign']).created[0].id
    store.start(foreign)
    executor._running[foreign] = Foreign()
    job, record = started(rig, store, executor, 'wait=x')
    with pytest.raises(DownloadinuxError) as error:
        executor.cancel([foreign, job])
    assert str(error.value) == f'cannot signal the runner of job {foreign}: {os.strerror(errno.EPERM)}'
    assert store.get(foreign).cancel_requested and store.get(job).cancel_requested
    wait_for(lambda: rig.signals(record) == [signal.SIGTERM])
    [ended] = poll_until(executor, 1)
    assert (ended.id, ended.state) == (job, 'cancelled')
    del executor._running[foreign]


def test_cancel_ends_the_runner_its_downloader_and_grandchild_and_removes_the_snapshot(tmp_path, monkeypatch):
    """The process tree through the real runner (DEC-36, DEC-48)."""
    cookies, state = roots(tmp_path)
    bin_, log, downloads = tmp_path / 'tree-bin', tmp_path / 'stub-log', tmp_path / 'downloads'
    for directory in (bin_, log, downloads):
        directory.mkdir()
    runner = bin_ / we.RUNNER
    runner.write_text(f'#!{sys.executable} -I\nimport sys\nsys.path.insert(0, {str(SRC)!r})\n'
                      'from downloadinux.cookie_runner import main\nsys.exit(main())\n')
    runner.chmod(0o755)
    for name in ('yt-dlp', 'gallery-dl'):
        (bin_ / name).write_text(STUB.replace('{python}', sys.executable))
        (bin_ / name).chmod(0o755)
    for name, value in (('PATH', str(bin_)), ('STUB_LOG', str(log)), ('STUB_MODE', 'grandchild,wait'),
                        ('STUB_GC_MODE', 'wait')):
        monkeypatch.setenv(name, value)
    store = js.open_store(state, clock=lambda: NOW)
    executor = we.Executor(store, cookies=cookies, state=state, downloads=downloads)
    try:
        job = store.submit('video', ['https://example.com/v'], source='alpha', revision=ALPHA,
                           user_agent=UA).created[0].id
        executor.launch(store.start(job))
        wait_for(lambda: len(list(log.glob('yt-dlp-child-*.json'))) == 1
                 and len(list(log.glob('yt-dlp-grandchild-*.json'))) == 1)
        [child] = [json.loads(p.read_text()) for p in log.glob('yt-dlp-child-*.json')]
        [grandchild] = [json.loads(p.read_text()) for p in log.glob('yt-dlp-grandchild-*.json')]
        jar = Path(child['jar'])
        # The private snapshot exists during the run; no argv names the shared store.
        assert jar.is_file() and jar.parent.parent == state / cookie_snapshot.DIRECTORY
        runner_args = we.runner_argv(store.get(job), cookies, state)
        assert str(cookies) not in ' '.join(child['argv'] + grandchild['argv'])
        assert not [item for item in runner_args + child['argv'] if 'cookies.txt' in item and item != str(jar)]
        assert child['pgid'] == child['pid'] and grandchild['pgid'] == child['pgid']
        assert executor.cancel([job]) == (js.CancelOutcome(job, js.SIGNAL, 'running'),)
        [ended] = poll_until(executor, 1)
        assert (ended.state, ended.exit_status) == ('cancelled', 143)
        for record in (child, grandchild):
            role = 'child' if record is child else 'grandchild'
            signals = (log / f'yt-dlp-{role}-{record["pid"]}.signals').read_text().split()
            assert signals == [str(int(signal.SIGTERM))]
            wait_for(lambda: not alive(record['pid']))
        assert not jar.exists() and not jar.parent.exists()
        assert store.logs.joinpath(js.log_name(job, 1)).read_bytes() == b''
    finally:
        (log / 'release').touch()
        (log / 'release-gc').touch()
        for process in executor._running.values():
            process.kill()
            process.wait()
        store.close()


# Shutdown (§5 *Shutdown*).

class Clock:
    """Fake monotonic time and sleep for the shutdown wait.

    Every read costs `drift` seconds, as real polls do, so the wait is not a
    whole number of tenths and its last sleep must be cut short. Each sleep
    first waits, in real time, until every registered runner but the
    `stubborn` ones has exited, so the fake 20 seconds never outrun a real
    exit; then it advances the clock by exactly the requested amount. The
    deadline is the first read after `arm`: the moment of the signals.
    """

    def __init__(self, start=1000.0, drift=0.013):
        self.now = start
        self.drift = drift
        self.sleeps = []
        self.executor = None
        self.stubborn = set()
        self.armed = False
        self.deadline = None
        self.read = None

    def arm(self):
        self.armed = True

    def monotonic(self):
        value = self.read = self.now
        if self.armed and self.deadline is None:
            self.deadline = value + 20
        self.now += self.drift
        return value

    def sleep(self, seconds):
        assert 0 < seconds <= 0.1
        assert self.read + seconds <= self.deadline + 1e-9  # never past the deadline
        wait_for(lambda: all(process.poll() is not None
                             for job, process in self.executor._running.items() if job not in self.stubborn))
        self.sleeps.append(seconds)
        self.now += seconds


def test_shutdown_signals_waits_at_most_20_seconds_and_marks_the_rest(rig):
    clock = Clock()
    store = rig.open(delay=lambda: 11)
    executor = rig.executor(store, monotonic=clock.monotonic, sleep=clock.sleep)
    clock.executor = executor
    run = rig.scheduler(store, executor)
    interrupted, a = started(rig, store, executor, 'wait=x')
    succeeded, b = started(rig, store, executor, 'wait=x,term=exit0')
    cancelled, c = started(rig, store, executor, 'wait=x,term=ignore1')
    executor.cancel([cancelled])
    wait_for(lambda: rig.signals(c) == [signal.SIGTERM])
    stubborn = store.submit('video', ['wait=x,term=ignore'], lane='video').created[0].id
    executor.launch(store.start(stubborn))
    d = rig.record('wait=x,term=ignore')
    unlaunched = store.submit('gallery', ['unlaunched'], lane='gallery').created[0].id
    store.start(unlaunched)
    queued = store.submit('gallery', ['queued']).created[0].id
    clock.stubborn = {stubborn}
    clock.arm()
    rig.wall.advance(3)
    marked = executor.shutdown(run)
    assert marked == (stubborn, unlaunched)
    assert run.stopping
    # The last sleep was cut to land on the deadline, polled once more there.
    assert 0 < clock.sleeps[-1] < 0.1
    assert clock.deadline <= clock.read <= clock.deadline + clock.drift + 1e-9
    assert len(clock.sleeps) >= 150
    states = {job: store.get(job) for job in (interrupted, succeeded, cancelled, stubborn, unlaunched)}
    assert (states[interrupted].state, states[interrupted].exit_status, states[interrupted].error) == \
        ('interrupted', 143, 'worker stopped')
    assert (states[succeeded].state, states[succeeded].exit_status) == ('succeeded', 0)
    assert (states[cancelled].state, states[cancelled].exit_status) == ('cancelled', 143)
    for job in (stubborn, unlaunched):
        assert (states[job].state, states[job].exit_status, states[job].error) == \
            ('interrupted', None, 'worker stopped')
    assert deadline(store, 'video') == deadline(store, 'gallery') == '2026-10-12T12:00:14Z'
    assert [rig.signals(r) for r in (a, b, d)] == [[signal.SIGTERM]] * 3
    assert rig.signals(c) == [signal.SIGTERM] * 2
    # Nothing starts during or after the wait, and the stubborn runner is the test's to kill.
    assert store.get(queued).state == 'queued' and run.run_pass() == ()
    assert len(rig.records()) == 4
    assert alive(d['pid'])
    process = executor._running.pop(stubborn)
    process.kill()
    process.wait()


def test_after_shutdown_every_call_is_refused_and_changes_nothing(rig):
    store = rig.open()
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    queued = store.submit('video', ['queued']).created[0].id
    assert executor.shutdown(run) == ()
    before = store.list()
    for call in (lambda: executor.launch(store.get(queued)), executor.poll, lambda: executor.cancel([queued]),
                 lambda: executor.shutdown(run)):
        with pytest.raises(DownloadinuxError) as error:
            call()
        assert str(error.value) == 'the worker has stopped'
    assert store.list() == before
    assert not store.logs.joinpath(js.log_name(queued, 1)).exists()


def test_shutdown_with_a_prompt_runner_returns_well_within_the_bound(rig):
    store = rig.open()
    executor = rig.executor(store)
    job, _ = started(rig, store, executor, 'wait=x')
    begun = time.monotonic()
    assert executor.shutdown(rig.scheduler(store, executor)) == ()
    assert time.monotonic() - begun < 5
    assert (store.get(job).state, store.get(job).exit_status) == ('interrupted', 143)


@pytest.mark.skipif(os.geteuid() == 0, reason='root may signal any process')
def test_a_shutdown_signal_error_is_raised_after_the_wait_and_the_mark(rig):
    clock = Clock()
    store = rig.open()
    executor = rig.executor(store, monotonic=clock.monotonic, sleep=clock.sleep)
    clock.executor = executor
    foreign = store.submit('video', ['foreign']).created[0].id
    store.start(foreign)
    executor._running[foreign] = Foreign()
    clock.stubborn = {foreign}
    clock.arm()
    job, record = started(rig, store, executor, 'wait=x')
    with pytest.raises(DownloadinuxError) as error:
        executor.shutdown(rig.scheduler(store, executor))
    assert str(error.value) == f'cannot signal the runner of job {foreign}: {os.strerror(errno.EPERM)}'
    assert rig.signals(record) == [signal.SIGTERM]
    assert (store.get(job).state, store.get(job).exit_status) == ('interrupted', 143)
    assert (store.get(foreign).state, store.get(foreign).error) == ('interrupted', 'worker stopped')
    with pytest.raises(DownloadinuxError, match='^the worker has stopped$'):
        executor.poll()
    del executor._running[foreign]


# End-to-end D1 evidence: store, scheduler, and executor together.

def test_lanes_and_immediate_queues_run_together_through_ends_retries_and_a_restart(rig):
    seam = ws.throttle_override({ws.THROTTLE_VARIABLE: '0'})  # zero-delay draws
    store = rig.open(delay=seam)
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    stamp = lambda: rig.wall.now.strftime(js.TIMESTAMP_FORMAT)
    v1, v2 = (j.id for j in store.submit('video', ['v1,wait=v1', 'v2'], lane='video').created)
    g1, g2 = (j.id for j in store.submit('gallery', ['g1,output,wait=g1,exit=2', 'g2,wait=g2'],
                                          lane='gallery').created)
    a1, a2 = (j.id for j in store.submit('video', ['a1,wait=a1', 'a2'], user_agent=UA).created)
    b1 = store.submit('gallery', ['b1,wait=b1'], source='alpha', revision=ALPHA).created[0].id
    # Both lanes and both immediate submissions run at once, one job per queue.
    assert run.run_pass() == (g1, v1, a1, b1)
    assert executor.running == (v1, g1, a1, b1)
    assert run.run_pass() == ()
    assert [store.get(j).state for j in (v2, g2, a2)] == ['queued'] * 3

    # A lane end draws its deadline (zero delay: the end itself); a retry
    # rejoins the back of its lane, behind g2.
    rig.wall.advance(10)
    rig.release('g1')
    [ended] = poll_until(executor, 1)
    assert (ended.id, ended.state, ended.exit_status) == (g1, 'failed', 2)
    assert deadline(store, 'gallery') == ended.finished_at == stamp()
    assert store.retry([(g1, None, None, '')]).outcomes[0].outcome == js.REQUEUED
    assert run.run_pass() == (g2,)

    # An immediate end draws no lane deadline; its queue's next job follows.
    rig.wall.advance(10)
    lanes = deadline(store, 'video'), deadline(store, 'gallery')
    rig.release('a1')
    assert [r.id for r in poll_until(executor, 1)] == [a1]
    assert (deadline(store, 'video'), deadline(store, 'gallery')) == lanes
    assert run.run_pass() == (a2,)
    poll_until(executor, 1)

    rig.wall.advance(10)
    rig.release('v1')
    assert [r.id for r in poll_until(executor, 1)] == [v1]
    assert deadline(store, 'video') == stamp()
    assert run.run_pass() == (v2,)
    poll_until(executor, 1)
    assert deadline(store, 'video') == stamp()

    rig.wall.advance(10)
    rig.release('g2')
    assert [r.id for r in poll_until(executor, 1)] == [g2]
    assert run.run_pass() == (g1,)
    [again] = poll_until(executor, 1)
    assert (again.id, again.attempt, again.state, again.exit_status) == (g1, 2, 'failed', 2)
    rig.release('b1')
    assert [r.state for r in poll_until(executor, 1)] == ['succeeded']
    # Both attempts' logs are kept, each with its own bytes.
    first, second = log_path(rig, g1, 1), log_path(rig, g1, 2)
    assert first.read_bytes() == second.read_bytes() == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'

    # A worker that stops without shutdown leaves a running job; the next
    # store reconciles it, and the queue behind it runs after.
    r1, r2 = (j.id for j in store.submit('video', ['r1,wait=r1', 'r2'], lane='video').created)
    rig.wall.advance(10)
    assert run.run_pass() == (r1,)
    process = executor._running.pop(r1)
    store.close()
    process.kill()
    process.wait()
    rig.wall.advance(10)
    store = rig.open(delay=seam)
    executor = rig.executor(store)
    run = rig.scheduler(store, executor)
    assert store.reconcile() == (r1,)
    record = store.get(r1)
    assert (record.state, record.error, record.exit_status) == ('interrupted', 'worker restarted', None)
    assert deadline(store, 'video') == stamp()
    assert run.run_pass() == (r2,)
    assert [r.state for r in poll_until(executor, 1)] == ['succeeded']
    assert executor.shutdown(run) == ()


# Standard-library boundary (§1, DEC-41; §5, DEC-48).

BOUNDARY = '''
import importlib.abc, sys
ALLOWED = ('downloadinux', 'downloadinux.worker_execution', 'downloadinux.worker_scheduler',
           'downloadinux.nordvpn_output', 'downloadinux.job_store', 'downloadinux.errors',
           'downloadinux.cookie_store', 'downloadinux.cookiejar')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split('.')[0]
        if top not in sys.stdlib_module_names and top != 'downloadinux':
            raise ImportError('non-stdlib import: ' + name)
        if top == 'downloadinux' and name not in ALLOWED:
            raise ImportError('host import: ' + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
import time
from datetime import datetime, timezone
from pathlib import Path
from downloadinux import job_store as js, worker_execution as we, worker_scheduler as ws
for blocked in ('downloadinux.launch', 'downloadinux.cookie_runner', 'downloadinux.settings', 'tomlkit'):
    try:
        __import__(blocked)
    except ImportError:
        pass
    else:
        raise AssertionError('import not blocked: ' + blocked)
state, downloads = Path(sys.argv[2]), Path(sys.argv[3])
with js.open_store(state, clock=lambda: datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)) as store:
    job = store.submit('video', ['exit=0'], lane='video').created[0].id
    executor = we.Executor(store, cookies=Path('/cookies'), state=state, downloads=downloads)
    run = ws.Scheduler(store, executor.launch, vpn=False,
                       clock=lambda: datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc))
    assert run.run_pass() == (job,)
    deadline = time.monotonic() + 20
    while not executor.poll():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert store.get(job).state == 'succeeded'
    assert executor.shutdown(run) == ()
assert 'downloadinux.launch' not in sys.modules and 'downloadinux.cookie_runner' not in sys.modules
print('execution boundary passed')
'''


def test_isolated_stdlib_only_execution_launches_polls_and_shuts_down(rig):
    import subprocess
    root = rig.tmp / 'isolated-state'
    root.mkdir()
    result = subprocess.run(
        [sys.executable, '-I', '-c', BOUNDARY, str(SRC), str(root), str(rig.downloads)],
        capture_output=True, text=True, timeout=30,
        env={'PATH': str(rig.bin), 'HOME': str(rig.tmp / 'absent-home'), 'FIXTURE_LOG': str(rig.log)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'execution boundary passed\n'
    assert [r['url'] for r in rig.records()] == ['exit=0']
