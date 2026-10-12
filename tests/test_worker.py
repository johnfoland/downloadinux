"""The worker service and its clients (target design, Persistent worker and jobs §1; DEC-50, DEC-51).

Real stores, real fixture runners, and real `serve` processes taking real
signals. Unix socket paths are short (104 bytes on macOS), so every socket
lives under a directory made in `/tmp`, never under pytest's `tmp_path`.

Timeouts: the client's 30-second bound is exercised through `request`'s
`timeout` keyword (a fake listener that never replies, with a short
timeout); the worker's 10-second request read has no seam (DEC-50 names
none), so the stalled-client test waits the real 10 seconds.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from downloadinux import cookie_runner, job_store as js, worker
from downloadinux import worker_execution as we
from downloadinux.errors import ConfigError

from snapshot_support import SRC, wait_for
from test_worker_execution import FIXTURE as EXECUTION_FIXTURE, NOW, UA

# The execution tests' fixture runner, which also records, when it gets
# SIGTERM, whether FIXTURE_SOCKET still existed (`<pid>.socket`).
FIXTURE = EXECUTION_FIXTURE.replace(
    '    received.append(number)\n',
    "    with open(base + '.socket', 'a') as handle:\n"
    "        handle.write('%d\\n' % os.path.exists(os.environ.get('FIXTURE_SOCKET', '')))\n"
    '    received.append(number)\n', 1)
assert FIXTURE != EXECUTION_FIXTURE

# Runs `worker.run` with the test arguments `main` never passes: the socket
# path for every subcommand, and the download root for `serve`.
LAUNCHER = '''
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from downloadinux import worker
service = {'downloads': Path(sys.argv[3])} if sys.argv[4:5] == ['serve'] else {}
sys.exit(worker.run(sys.argv[4:], socket_path=Path(sys.argv[2]), **service))
'''

LISTENING = 'downloadinux-worker: listening on {}'
STOPPING = 'downloadinux-worker: stopping'
EMPTY_STATUS = b'{"version":1,"running":0,"queued":0}\n'
INVALID = b'{"version":1,"error":"invalid request"}\n'


class Serve:
    """A real `serve` process whose standard output and error go to files."""

    def __init__(self, process, out, err, socket_path):
        self.process = process
        self.out = out
        self.err = err
        self.socket = socket_path

    def lines(self):
        return self.out.read_text().splitlines()

    def stderr(self):
        return self.err.read_text()

    def wait_line(self, line, timeout=30):
        wait_for(lambda: line in self.lines() or self.process.poll() is not None, timeout)
        assert line in self.lines(), (self.lines(), self.stderr())

    def listening(self):
        self.wait_line(LISTENING.format(self.socket))
        return self

    def stop(self, number=signal.SIGTERM, timeout=40):
        if self.process.poll() is None:
            self.process.send_signal(number)
        return self.process.wait(timeout)


class Rig:
    """Roots, a fixture runner on `PATH`, and a short socket directory."""

    def __init__(self, tmp_path, short, monkeypatch):
        self.tmp = tmp_path
        self.short = short
        self.state = tmp_path / 'state'
        self.cookies = tmp_path / 'cookies'
        self.downloads = tmp_path / 'downloads'
        self.log = tmp_path / 'fixture-log'
        self.bin = tmp_path / 'bin'
        for directory in (self.state, self.cookies, self.downloads, self.log, self.bin):
            directory.mkdir()
        self.socket = short / 'w.sock'
        self.runner = self.bin / we.RUNNER
        self.runner.write_text(FIXTURE.replace('{python}', sys.executable))
        self.runner.chmod(0o755)
        # In-process services launch runners with this process's environment.
        monkeypatch.setenv('PATH', str(self.bin))
        monkeypatch.setenv('FIXTURE_LOG', str(self.log))
        monkeypatch.setenv('FIXTURE_SOCKET', str(self.socket))
        self.serves = []
        self.count = 0

    def env(self, **extra):
        env = {'PATH': str(self.bin), 'HOME': str(self.tmp / 'absent-home'), 'FIXTURE_LOG': str(self.log),
               'FIXTURE_SOCKET': str(self.socket), 'DOWNLOADINUX_TEST_THROTTLE_SECONDS': '0'}
        env.update(extra)
        return {key: value for key, value in env.items() if value is not None}

    def start(self, *, socket_path=None, state=None, env=None, umask=0o022, args=None):
        socket_path = self.socket if socket_path is None else socket_path
        self.count += 1
        out, err = self.tmp / f'serve-{self.count}.out', self.tmp / f'serve-{self.count}.err'
        if args is None:
            args = ['--cookies', str(self.cookies), '--state', str(self.state if state is None else state)]
        with open(out, 'wb') as stdout, open(err, 'wb') as stderr:
            process = subprocess.Popen(
                [sys.executable, '-I', '-c', LAUNCHER, str(SRC), str(socket_path), str(self.downloads),
                 'serve', *args],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=self.env() if env is None else env,
                umask=umask)
        serve = Serve(process, out, err, socket_path)
        self.serves.append(serve)
        return serve

    def client(self, *args, stdin=b'', socket_path=None, timeout=60):
        socket_path = self.socket if socket_path is None else socket_path
        return subprocess.run(
            [sys.executable, '-I', '-c', LAUNCHER, str(SRC), str(socket_path), str(self.downloads), *args],
            input=stdin, capture_output=True, env=self.env(), timeout=timeout)

    def open(self, **kwargs):
        kwargs.setdefault('clock', lambda: NOW)
        store = js.open_store(self.state, **kwargs)
        assert isinstance(store, js.JobStore)
        return store

    def records(self):
        return [json.loads(path.read_text()) for path in sorted(self.log.glob('*.json'))]

    def record(self, url):
        wait_for(lambda: any(r['url'] == url for r in self.records()))
        [record] = [r for r in self.records() if r['url'] == url]
        return record

    def received(self, record):
        """The SIGTERMs a fixture got, each with whether the socket still existed then."""
        signals = self.log / f'{record["pid"]}.signals'
        sockets = self.log / f'{record["pid"]}.socket'
        if not signals.exists():
            return []
        return list(zip(map(int, signals.read_text().split()), map(int, sockets.read_text().split())))

    def release(self, name='all'):
        (self.log / f'release-{name}').touch()

    def close(self):
        self.release()
        for serve in self.serves:
            if serve.process.poll() is None:
                serve.process.kill()
                serve.process.wait()


@pytest.fixture
def short():
    path = Path(tempfile.mkdtemp(prefix='dlw-', dir='/tmp'))
    yield path
    for root, directories, _ in os.walk(path):
        for name in directories:
            os.chmod(os.path.join(root, name), 0o700)
    os.chmod(path, 0o700)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def rig(tmp_path, short, monkeypatch):
    rig = Rig(tmp_path, short, monkeypatch)
    yield rig
    rig.close()


@pytest.fixture
def quiet_signals():
    """Harmless SIGTERM/SIGINT handlers around an in-process service.

    The service restores whatever was installed before it, so a stop sent
    after it has returned lands here rather than on the test process.
    """
    previous = {number: signal.signal(number, lambda *_: None) for number in (signal.SIGTERM, signal.SIGINT)}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


def ask(socket_path, payload, timeout=15):
    """Send raw request bytes the way the client does; return the raw reply bytes."""
    return worker._exchange(socket_path, payload, timeout)


def request_bytes(op, args):
    return worker.encode({'version': 1, 'op': op, 'args': args})


def status(socket_path):
    return json.loads(ask(socket_path, request_bytes('status', {})))


class InProcess:
    """Runs `worker.serve` on this (main) thread while `drive` runs on another.

    The driver's thread ends the service with a real `SIGTERM` to this
    process (or the signal it names). Log lines are collected in `lines`.
    """

    def __init__(self, rig, monkeypatch):
        self.rig = rig
        self.lines = []
        monkeypatch.setattr(worker, '_log', lambda text: self.lines.append(worker.LOG_PREFIX + text))

    def run(self, drive, *, number=signal.SIGTERM, **kwargs):
        kwargs.setdefault('socket_path', self.rig.socket)
        kwargs.setdefault('downloads', self.rig.downloads)
        kwargs.setdefault('environ', {'DOWNLOADINUX_TEST_THROTTLE_SECONDS': '0'})
        failure = []

        def driver():
            try:
                drive()
            except BaseException as error:
                failure.append(error)
            finally:
                os.kill(os.getpid(), number)
        thread = threading.Thread(target=driver)
        thread.start()
        try:
            result = worker.serve(str(self.rig.cookies), str(self.rig.state), **kwargs)
        finally:
            thread.join(60)
        if failure:
            raise failure[0]
        return result

    def wait_line(self, line, timeout=30):
        wait_for(lambda: line in self.lines, timeout)


@pytest.fixture
def inprocess(rig, monkeypatch, quiet_signals):
    return InProcess(rig, monkeypatch)


# Usage (DEC-51 *Exit status and output*).

USAGE = [
    ([], 'a subcommand is required: serve, status, submit, or show'),
    (['nope'], "unknown subcommand 'nope'; use serve, status, submit, or show"),
    (['--state'], "unknown subcommand '--state'; use serve, status, submit, or show"),
    (['status', 'extra'], "status: unexpected operand 'extra'"),
    (['status', '--json'], "status: unexpected operand '--json'"),
    (['serve'], 'serve: --cookies is required'),
    (['serve', '--state', 's'], 'serve: --cookies is required'),
    (['serve', '--cookies', 'c'], 'serve: --state is required'),
    (['serve', '--cookies'], 'serve: --cookies needs a value'),
    (['serve', '--cookies', 'c', '--state'], 'serve: --state needs a value'),
    (['serve', '--cookies', 'c', '--cookies', 'd', '--state', 's'], 'serve: --cookies given more than once'),
    (['serve', '--state', 's', '--cookies', 'c', '--state', 't'], 'serve: --state given more than once'),
    (['serve', '--socket', 'x', '--cookies', 'c', '--state', 's'], "serve: unknown option '--socket'"),
    (['serve', '--cookies', '', '--state', 's'], 'serve: --cookies must name a directory'),
    (['serve', '--cookies', 'c', '--state', ''], 'serve: --state must name a directory'),
    (['serve', '--cookies', 'c', '--state', 's', 'extra'], "serve: unexpected operand 'extra'"),
    (['serve', 'extra', '--cookies', 'c', '--state', 's'], 'serve: --cookies is required'),
]


@pytest.mark.parametrize('argv, reason', USAGE)
def test_usage_errors_exit_2_with_nothing_on_stdout(rig, capsys, argv, reason):
    contacted = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(rig.socket))
    listener.listen()
    listener.setblocking(False)
    try:
        assert worker.run(argv, socket_path=rig.socket) == 2
        try:
            contacted.append(listener.accept())
        except BlockingIOError:
            pass
    finally:
        listener.close()
    assert contacted == []
    assert capsys.readouterr() == ('', f'downloadinux: error: {reason}\n')
    assert not (rig.state / 'jobs').exists()


def test_main_reads_sys_argv(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['downloadinux-worker', 'status', 'x'])
    assert worker.main() == 2
    assert capsys.readouterr().err == "downloadinux: error: status: unexpected operand 'x'\n"


@pytest.mark.parametrize('rest, runner', [
    (['--cookies'], ['download', '--cookies']),
    (['--cookies', 'c', '--cookies', 'd'], ['download', '--cookies', 'c', '--cookies', 'd']),
    (['--bogus'], ['download', '--bogus']),
    (['--state', 's'], ['download', '--state', 's']),
    (['--cookies', 'c'], ['cleanup']),
    (['--cookies', '', '--state', 's'], ['download', '--cookies', '', '--state', 's', '--user-agent', 'x']),
    (['--cookies', 'c', '--state', 's', 'extra'], ['cleanup', '--state', 's', 'extra']),
])
def test_serve_messages_are_the_runners_with_serve_as_the_subcommand(rest, runner):
    with pytest.raises(ConfigError) as expected:
        cookie_runner.parse(runner)
    with pytest.raises(ConfigError) as actual:
        worker.parse_serve(rest)
    assert str(actual.value) == str(expected.value).replace(runner[0] + ':', 'serve:', 1)


def test_serve_options_in_either_order_are_taken_verbatim():
    assert worker.parse_serve(['--state', '-s', '--cookies', '-c']) == ('-c', '-s')


# Start, status, and stop as a real process.

@pytest.mark.parametrize('number', [signal.SIGTERM, signal.SIGINT])
def test_a_real_serve_answers_status_and_stops_on_a_signal(rig, number):
    serve = rig.start(umask=0).listening()
    mode = os.lstat(rig.socket).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600
    result = rig.client('status')
    assert (result.returncode, result.stdout, result.stderr) == (0, EMPTY_STATUS, b'')
    assert serve.stop(number) == 0
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    assert serve.stderr() == ''
    assert not rig.socket.exists()
    assert stat.S_IMODE(os.stat(rig.state / 'jobs').st_mode) == 0o700


def test_status_counts_running_and_queued_jobs(rig):
    with rig.open() as store:
        store.submit('video', ['wait=a,term=ignore', 'exit=0'], lane='video', user_agent=UA)
        store.submit('gallery', ['wait=b'], user_agent=UA)
    serve = rig.start().listening()
    rig.record('wait=a,term=ignore')
    rig.record('wait=b')
    result = rig.client('status')
    assert result.stdout == b'{"version":1,"running":2,"queued":1}\n'
    rig.release()
    wait_for(lambda: rig.client('status').stdout == EMPTY_STATUS)
    assert serve.stop() == 0


def test_a_stop_closes_the_socket_before_signalling_runners(rig):
    """§5 *Shutdown*: the socket is gone before any runner gets SIGTERM, and stays gone."""
    with rig.open() as store:
        [job] = store.submit('video', ['wait=a,term=ignore,exit=1'], lane='video', user_agent=UA).created
    serve = rig.start().listening()
    record = rig.record('wait=a,term=ignore,exit=1')
    serve.process.send_signal(signal.SIGTERM)
    wait_for(lambda: rig.received(record))
    assert rig.received(record) == [(signal.SIGTERM, 0)]
    unreachable = rig.client('status')
    assert unreachable.returncode == 3 and unreachable.stdout == b''
    assert serve.process.poll() is None  # still in the shutdown wait
    rig.release('a')
    assert serve.process.wait(30) == 0
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    with rig.open() as store:
        ended = store.get(job.id)
    assert (ended.state, ended.error, ended.exit_status) == ('interrupted', 'worker stopped', 1)


def test_reconciliation_marks_running_jobs_and_logs_the_count(rig):
    with rig.open() as store:
        store.submit('video', ['exit=0'], lane='video', user_agent=UA)
        store.submit('gallery', ['exit=0'], user_agent=UA)
        for head in store.queue_heads():
            store.start(head.head.id)
    serve = rig.start().listening()
    assert serve.lines()[0] == 'downloadinux-worker: marked 2 running jobs interrupted (worker restarted)'
    assert serve.stop() == 0
    with rig.open() as store:
        assert {(r.state, r.error) for r in store.list()} == {('interrupted', 'worker restarted')}


# One worker per state root.

def test_a_second_worker_waits_without_a_socket_or_store_access_then_stops(rig):
    first = rig.start().listening()
    database = rig.state / 'jobs' / 'jobs.sqlite3'
    before = (database.read_bytes(), os.stat(database).st_mtime_ns, sorted(os.listdir(rig.state / 'jobs')))
    second_socket = rig.short / 'x.sock'
    second = rig.start(socket_path=second_socket)
    waiting = f'downloadinux-worker: another worker holds {rig.state}/jobs/.worker.lock; waiting'
    second.wait_line(waiting)
    time.sleep(1.5)  # at least one retry, still without a socket or a second line
    assert not second_socket.exists()
    assert second.stop() == 0
    assert second.lines() == [waiting]
    assert second.stderr() == ''
    assert (database.read_bytes(), os.stat(database).st_mtime_ns, sorted(os.listdir(rig.state / 'jobs'))) == before
    assert first.process.poll() is None
    assert first.stop() == 0


def test_a_waiting_worker_takes_over_when_the_owner_exits(rig):
    first = rig.start().listening()
    second_socket = rig.short / 'x.sock'
    second = rig.start(socket_path=second_socket)
    second.wait_line(f'downloadinux-worker: another worker holds {rig.state}/jobs/.worker.lock; waiting')
    assert first.stop() == 0
    second.listening()
    assert rig.client('status', socket_path=second_socket).stdout == EMPTY_STATUS
    assert second.stop() == 0


def test_the_waiting_line_repeats_only_every_60_seconds(rig, inprocess):
    holder = rig.open()
    clock = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += 0.5 if len(sleeps) % 2 else 0.6  # 1.1 s per two sleeps
        if len(sleeps) == 250:  # 137.5 s
            signal.raise_signal(signal.SIGTERM)

    status_code = worker.serve(str(rig.cookies), str(rig.state), socket_path=rig.socket,
                               environ={}, monotonic=lambda: clock[0], sleep=sleep)
    holder.close()
    assert status_code == 0
    assert set(sleeps) == {1}
    # Attempts at 100.0, then each 0.5/0.6 s: lines at 100.0, 160.0+ and 220.0+.
    line = f'downloadinux-worker: another worker holds {rig.state}/jobs/.worker.lock; waiting'
    assert inprocess.lines == [line, line, line]
    assert not rig.socket.exists()


# Startup failures (DEC-50 *Start*).

def test_a_stale_socket_is_replaced(rig):
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(rig.socket))
    dead.close()  # a killed worker's socket file: nothing listens
    assert stat.S_ISSOCK(os.lstat(rig.socket).st_mode)
    refused = rig.client('status')
    assert refused.stderr == f'downloadinux: error: the worker is not answering: {rig.socket}: Connection refused\n'.encode()
    serve = rig.start().listening()
    assert rig.client('status').stdout == EMPTY_STATUS
    assert serve.stop() == 0


def plant_file(path):
    path.write_text('not a socket')


def plant_directory(path):
    path.mkdir()


@pytest.mark.parametrize('plant', [plant_file, plant_directory])
def test_anything_else_at_the_socket_path_fails_startup(rig, plant):
    plant(rig.socket)
    serve = rig.start()
    assert serve.process.wait(30) == 1
    assert serve.lines() == []
    assert serve.stderr() == f'downloadinux: error: {rig.socket}: exists and is not a socket\n'
    assert rig.socket.exists() and not stat.S_ISSOCK(os.lstat(rig.socket).st_mode)
    rig.open().close()  # the store was closed and its ownership released


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_an_unwritable_socket_directory_fails_startup(rig):
    directory = rig.short / 'locked'
    directory.mkdir(mode=0o500)
    serve = rig.start(socket_path=directory / 'w.sock')
    assert serve.process.wait(30) == 1
    assert serve.stderr() == f'downloadinux: error: {directory / "w.sock"}: Permission denied\n'
    assert serve.lines() == []


def test_a_missing_socket_directory_fails_startup(rig):
    path = rig.short / 'absent' / 'w.sock'
    serve = rig.start(socket_path=path)
    assert serve.process.wait(30) == 1
    assert serve.stderr() == f'downloadinux: error: {path}: No such file or directory\n'


@pytest.mark.parametrize('value', ['', '290', '-1', '1.5', ' 1', '١'])
def test_an_invalid_throttle_seam_fails_startup_before_the_store(rig, value):
    serve = rig.start(env=rig.env(DOWNLOADINUX_TEST_THROTTLE_SECONDS=value))
    assert serve.process.wait(30) == 1
    assert serve.stderr() == ('downloadinux: error: invalid DOWNLOADINUX_TEST_THROTTLE_SECONDS;'
                              ' expected an integer from 0 to 289\n')
    assert serve.lines() == []
    assert not (rig.state / 'jobs').exists()


def test_a_missing_state_root_fails_startup(rig):
    serve = rig.start(state=rig.tmp / 'absent')
    assert serve.process.wait(30) == 1
    assert serve.stderr() == f'downloadinux: error: {rig.tmp / "absent"}: No such file or directory\n'
    assert not rig.socket.exists()


# The client (DEC-51 *Exit status and output*, *Output form*).

def test_a_client_with_no_socket_file_exits_3(rig):
    result = rig.client('status')
    assert result.returncode == 3 and result.stdout == b''
    assert result.stderr == f'downloadinux: error: the worker is not answering: {rig.socket}: No such file or directory\n'.encode()


class FakeWorker:
    """A listener that reads one request to EOF, then sends `reply` (or nothing) and closes."""

    def __init__(self, path, reply=None, *, hold=False):
        self.path = path
        self.reply = reply
        self.hold = hold
        self.received = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        connection, _ = self.listener.accept()
        with connection:
            chunks = []
            while chunk := connection.recv(1 << 16):
                chunks.append(chunk)
            self.received = b''.join(chunks)
            if self.hold:
                self.done.wait(30)
                return
            if self.reply is not None:
                try:
                    connection.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self.done.set()
        self.thread.join(30)
        self.listener.close()


def fake_request(path, reply, capsys, *, timeout=5):
    fake = FakeWorker(path, reply)
    try:
        code = worker.request(path, 'status', {}, timeout=timeout)
    finally:
        fake.close()
    return code, capsys.readouterr(), fake.received


UNEXPECTED = [
    b'',
    b'not json',
    b'[1]\n',
    b'"text"\n',
    b'{"version":2}\n',
    b'{"version":"1"}\n',
    b'{"version":true}\n',
    b'{"version":1.0}\n',
    b'{"running":0}\n',
    b'{"version":1,"error":3}\n',
    b'{"version":1,"error":null}\n',
    b'{"version":1,"error":"x","running":0}\n',
    b'{"version":1,"version":1}\n',
    b'{"version":1,"running":0,"running":1}\n',
    b'{"version":1,"running":NaN}\n',
    b'{"version":1,"running":1e999}\n',
    b'{"version":1,"x":"\xff"}\n',
    b'{"version":1,"x":' + b'[' * 100000 + b']' * 100000 + b'}\n',
    b'{"version":1} {"version":1}\n',
    b'\xef\xbb\xbf{"version":1}\n',
    b'{"version":1,"pad":"' + b'x' * worker.LIMIT + b'"}\n',
]


@pytest.mark.parametrize('reply', UNEXPECTED, ids=range(len(UNEXPECTED)))
def test_an_unexpected_reply_exits_3(rig, capsys, reply):
    code, output, received = fake_request(rig.socket, reply, capsys)
    assert code == 3
    assert output == ('', 'downloadinux: error: the worker is not answering: unexpected reply\n')
    assert received == b'{"version":1,"op":"status","args":{}}\n'


@pytest.mark.parametrize('reply, printed, code', [
    (b'{"version":1,"running":0,"queued":0}', '{"version":1,"running":0,"queued":0}\n', 0),
    (b' {"queued": 2, "version": 1, "running": 1}\n\n', '{"version":1,"queued":2,"running":1}\n', 0),
    (b'{"version":1,"error":"unknown job ID 9"}\n', '{"version":1,"error":"unknown job ID 9"}\n', 1),
    (b'{"error":"caf\xc3\xa9 \\ud83d\\ude00","version":1}', '{"version":1,"error":"caf\\u00e9 \\ud83d\\ude00"}\n', 1),
])
def test_an_accepted_reply_is_reprinted_in_the_output_form(rig, capsys, reply, printed, code):
    result, output, _ = fake_request(rig.socket, reply, capsys)
    assert (result, output.out, output.err) == (code, printed, '')


def test_a_worker_that_never_replies_exits_3_at_the_client_timeout(rig, capsys):
    fake = FakeWorker(rig.socket, hold=True)
    started = time.monotonic()
    try:
        code = worker.request(rig.socket, 'status', {}, timeout=1)
    finally:
        fake.close()
    assert 0.9 < time.monotonic() - started < 10
    assert code == 3
    assert capsys.readouterr() == ('', 'downloadinux: error: the worker is not answering: no reply within 30 seconds\n')


def test_the_client_allows_30_seconds(monkeypatch):
    import inspect
    assert worker.CLIENT_TIMEOUT == 30
    assert inspect.signature(worker.request).parameters['timeout'].default == 30
    assert inspect.signature(worker.run).parameters['timeout'].default == 30


# The server's request handling (DEC-51 *Wire protocol*).

@pytest.fixture
def serving(rig):
    serve = rig.start().listening()
    yield serve
    assert serve.stop() == 0
    assert serve.stderr() == ''


BAD_REQUESTS = [
    b'',
    b'\xff\xfe',
    b'{"version":1,"op":"status","args":{"x":"\xff"}}',
    b'{"version":1,"op":"status","args":{}}{}',
    b'[]',
    b'null',
    b'{"version":1,"op":"status","args":{},"extra":0}',
    b'{"version":1,"op":"status"}',
    b'{"op":"status","args":{}}',
    b'{"version":1,"version":1,"op":"status","args":{}}',
    b'{"version":1,"op":"status","args":{"a":1,"a":1}}',
    b'{"version":NaN,"op":"status","args":{}}',
    b'{"version":1,"op":"status","args":{"n":Infinity}}',
    b'{"version":1,"op":"status","args":{"n":-Infinity}}',
    b'{"version":1,"op":"status","args":{"n":1e999}}',
    b'{"version":1,"op":"status","args":' + b'[' * 100000 + b']' * 100000 + b'}',
    b'{"version":true,"op":"status","args":{}}',
    b'{"version":1.0,"op":"status","args":{}}',
    b'{"version":"1","op":"status","args":{}}',
    b'{"version":1,"op":"bogus","args":{}}',
    b'{"version":1,"op":3,"args":{}}',
    b'{"version":1,"op":"status","args":[]}',
    b'{"version":1,"op":"status","args":null}',
    b'{"version":1,"op":"status","args":{"unexpected":1}}',
    b'{"version":1,"op":"status","args":{"\\ud800":1}}',
    b'{"version":1,"op":"status","args":{},"\\udfff":1}',
    b'{"version":1,"op":"stat\\udc80us","args":{}}',
    b'{"version":1,"op":"status","args":{"x":["\\ud83d"]}}',
    b'{"version":1,"op":"status","args":{"n":' + b'9' * 5000 + b'}}',
]


def test_malformed_requests_are_invalid(rig, serving):
    for payload in BAD_REQUESTS:
        assert ask(rig.socket, payload) == INVALID, payload[:80]
    assert ask(rig.socket, request_bytes('status', {})) == EMPTY_STATUS


def test_an_over_limit_request_is_invalid(rig, serving):
    assert ask(rig.socket, b' ' * (worker.LIMIT + 1)) == INVALID
    assert ask(rig.socket, b' ' * (worker.LIMIT - 40) + request_bytes('status', {})) == EMPTY_STATUS


@pytest.mark.parametrize('version', [0, 2, -1, 10 ** 30])
def test_another_integer_version_is_unsupported(rig, serving, version):
    payload = json.dumps({'version': version, 'op': 'status', 'args': {}}).encode()
    assert ask(rig.socket, payload) == b'{"version":1,"error":"unsupported request version"}\n'
    assert ask(rig.socket, json.dumps({'version': version}).encode()) == (
        b'{"version":1,"error":"unsupported request version"}\n')


def test_a_request_in_another_valid_spelling_is_served(rig, serving):
    assert ask(rig.socket, b'\n { "args" : { } , "op" : "status" , "version" : 1 } \n') == EMPTY_STATUS


def test_a_stalled_client_is_dropped_after_10_seconds_and_the_loop_carries_on(rig):
    with rig.open() as store:
        [job] = store.submit('gallery', ['wait=a'], user_agent=UA).created
    serve = rig.start().listening()
    rig.record('wait=a')
    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stalled.connect(str(rig.socket))
    stalled.sendall(b'{"version":1,')  # and never finishes or shuts down
    started = time.monotonic()
    rig.release('a')  # the runner exits while the worker waits on the stalled client
    stalled.settimeout(30)
    assert stalled.recv(100) == b''  # dropped, no reply
    elapsed = time.monotonic() - started
    stalled.close()
    assert 9 < elapsed < 15
    # The loop carried on: it polled the exit and still answers.
    wait_for(lambda: rig.client('status').stdout == EMPTY_STATUS)
    assert serve.stop() == 0
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    assert serve.stderr() == ''
    with rig.open() as store:
        assert store.get(job.id).state == 'succeeded'


# Failures in the loop (DEC-50, issue #74).

class StepBack:
    """A wall clock that steps back one second between the scheduler's read and the store's.

    The first read made by `JobStore.start` (through `_timestamp`) sees the
    step, as an NTP correction landing inside a pass would; every later
    read stays stepped back until the test moves it on.
    """

    def __init__(self, now):
        self.now = now
        self.stepped = False

    def __call__(self):
        if not self.stepped and sys._getframe(2).f_code.co_name == 'start':
            self.stepped = True
            self.now -= timedelta(seconds=1)
        return self.now


def spy_waits(monkeypatch, lines):
    real = js.JobStore.set_wait

    def set_wait(self, job_id, reason):
        lines.append(f'set_wait {job_id} {reason}')
        return real(self, job_id, reason)
    monkeypatch.setattr(js.JobStore, 'set_wait', set_wait)


def test_a_backward_clock_step_is_logged_once_and_the_next_pass_sets_the_throttle_wait(rig, inprocess, monkeypatch):
    """Issue #74's reproduction, through the service: the refusal is logged and clears."""
    with rig.open(clock=lambda: NOW - timedelta(seconds=130), delay=lambda: 130) as store:
        first, second = store.submit('video', ['exit=0', 'exit=0,n=2'], lane='video', user_agent=UA).created
        store.start(first.id)
        store.end(first.id, exit_status=0)  # the lane's deadline is now NOW
    clock = StepBack(NOW)
    spy_waits(monkeypatch, inprocess.lines)
    stamp = NOW.strftime(js.TIMESTAMP_FORMAT)
    failed = f'downloadinux-worker: scheduling failed: job {second.id} cannot start before its lane deadline {stamp}'

    def drive():
        inprocess.wait_line(failed)
        inprocess.wait_line(f'set_wait {second.id} throttle')
        time.sleep(1.2)  # more passes: the throttle wait holds and nothing is logged again
        assert status(rig.socket) == {'version': 1, 'running': 0, 'queued': 1}
        clock.now = NOW  # the clock catches up with the deadline
        rig.record('exit=0,n=2')
        wait_for(lambda: status(rig.socket) == {'version': 1, 'running': 0, 'queued': 0})

    assert inprocess.run(drive, clock=clock, environ={}) == 0
    assert clock.stepped
    assert inprocess.lines[:3] == [LISTENING.format(rig.socket), failed, f'set_wait {second.id} throttle']
    assert inprocess.lines.count(failed) == 1
    assert inprocess.lines[-1] == STOPPING
    with rig.open() as store:
        assert store.get(second.id).state == 'succeeded'


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_failed_end_is_logged_once_a_minute_and_a_later_poll_ends_the_job(rig, inprocess):
    with rig.open() as store:
        [job] = store.submit('gallery', ['wait=a'], user_agent=UA).created
    jobs = rig.state / 'jobs'
    mono = [1000.0]
    failed = f'downloadinux-worker: recording a runner exit failed: {jobs / "jobs.sqlite3"}: SQLite database operation failed'

    def drive():
        rig.record('wait=a')
        jobs.chmod(0o500)  # the rollback journal cannot be created: every end fails
        try:
            rig.release('a')
            inprocess.wait_line(failed)
            time.sleep(1.5)  # retried each iteration; the repeats are suppressed
            assert inprocess.lines.count(failed) == 1
            assert status(rig.socket) == {'version': 1, 'running': 1, 'queued': 0}  # still registered
            mono[0] += 59.9
            time.sleep(1.2)
            assert inprocess.lines.count(failed) == 1
            mono[0] += 0.1
            wait_for(lambda: inprocess.lines.count(failed) == 2)
        finally:
            jobs.chmod(0o700)
        wait_for(lambda: status(rig.socket) == {'version': 1, 'running': 0, 'queued': 0})

    assert inprocess.run(drive, monotonic=lambda: mono[0]) == 0
    assert inprocess.lines == [LISTENING.format(rig.socket), failed, failed, STOPPING]
    with rig.open() as store:
        ended = store.get(job.id)
    assert (ended.state, ended.exit_status) == ('succeeded', 0)


def test_identical_failure_lines_are_suppressed_but_different_ones_are_written(monkeypatch):
    lines, mono = [], [0.0]
    monkeypatch.setattr(worker, '_log', lines.append)
    repeats = worker._Repeats(lambda: mono[0])
    for text, at in (('a', 0), ('a', 59.9), ('b', 60), ('a', 61), ('a', 120.9), ('a', 121)):
        mono[0] = at
        repeats.write(text)
    assert lines == ['a', 'b', 'a', 'a']


def test_an_unexpected_launch_exception_is_an_internal_error(rig, inprocess, monkeypatch, capsys):
    with rig.open() as store:
        [running] = store.submit('gallery', ['wait=a'], lane='gallery', user_agent=UA).created
        [broken] = store.submit('video', ['boom'], lane='video', user_agent=UA).created
    real = we.Executor.launch

    def launch(self, record):
        if record.url == 'boom':
            raise RuntimeError('unexpected')
        return real(self, record)
    monkeypatch.setattr(we.Executor, 'launch', launch)

    assert inprocess.run(lambda: inprocess.wait_line(STOPPING)) == 1
    err = capsys.readouterr().err
    assert err.startswith('downloadinux: error: internal error\nTraceback (most recent call last):\n')
    assert err.endswith('RuntimeError: unexpected\n')
    assert inprocess.lines == [LISTENING.format(rig.socket), STOPPING]
    assert not rig.socket.exists()
    with rig.open() as store:
        launched, unlaunched = store.get(running.id), store.get(broken.id)
    assert (launched.state, launched.error, launched.exit_status) == ('interrupted', 'worker stopped', 128 + signal.SIGTERM)
    assert (unlaunched.state, unlaunched.error, unlaunched.exit_status) == ('interrupted', 'worker stopped', None)


# Stop (DEC-50 *Stop*).

@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_store_failure_in_the_shutdown_wait_exits_1_and_the_next_start_reconciles(rig):
    with rig.open() as store:
        [job] = store.submit('gallery', ['wait=a,term=ignore'], user_agent=UA).created
    jobs = rig.state / 'jobs'
    serve = rig.start().listening()
    record = rig.record('wait=a,term=ignore')
    jobs.chmod(0o500)
    try:
        serve.process.send_signal(signal.SIGTERM)
        wait_for(lambda: rig.received(record))
        rig.release('a')  # exits 0 inside the wait; its end cannot commit
        assert serve.process.wait(30) == 1
    finally:
        jobs.chmod(0o700)
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    assert serve.stderr() == f'downloadinux: error: {jobs / "jobs.sqlite3"}: SQLite database operation failed\n'
    assert not rig.socket.exists()
    with rig.open() as store:
        assert store.get(job.id).state == 'running'  # the mark never ran
    again = rig.start().listening()
    assert again.lines()[0] == 'downloadinux-worker: marked 1 running jobs interrupted (worker restarted)'
    assert again.stop() == 0
    with rig.open() as store:
        ended = store.get(job.id)
    assert (ended.state, ended.error) == ('interrupted', 'worker restarted')


def test_the_socket_path_is_removed_only_while_it_names_the_bound_socket(rig, inprocess):
    def drive():
        wait_for(rig.socket.exists)
        rig.socket.unlink()
        rig.socket.write_text('someone else')

    assert inprocess.run(drive) == 0
    assert rig.socket.read_text() == 'someone else'
    assert inprocess.lines == [LISTENING.format(rig.socket), STOPPING]


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_socket_removal_error_is_reported_and_the_shutdown_still_runs(rig):
    with rig.open() as store:
        [job] = store.submit('gallery', ['wait=a'], user_agent=UA).created
    directory = rig.short / 'run'
    directory.mkdir(mode=0o700)
    path = directory / 'w.sock'
    serve = rig.start(socket_path=path).listening()
    rig.record('wait=a')
    directory.chmod(0o500)
    try:
        assert serve.stop() == 1
    finally:
        directory.chmod(0o700)
    assert serve.stderr() == f'downloadinux: error: {path}: Permission denied\n'
    assert serve.lines() == [LISTENING.format(path), STOPPING]
    with rig.open() as store:
        ended = store.get(job.id)
    assert (ended.state, ended.error, ended.exit_status) == ('interrupted', 'worker stopped', 128 + signal.SIGTERM)


def test_repeated_stop_requests_while_stopping_change_nothing(rig):
    with rig.open() as store:
        [job] = store.submit('gallery', ['wait=a,term=ignore,exit=1'], user_agent=UA).created
    serve = rig.start().listening()
    record = rig.record('wait=a,term=ignore,exit=1')
    serve.process.send_signal(signal.SIGTERM)
    wait_for(lambda: rig.received(record))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGTERM):
        serve.process.send_signal(number)
    time.sleep(0.5)
    assert rig.received(record) == [(signal.SIGTERM, 0)]  # signalled once
    rig.release('a')
    assert serve.process.wait(30) == 0
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    with rig.open() as store:
        assert store.get(job.id).state == 'interrupted'


def test_a_stop_before_the_socket_opens_still_shuts_down(rig, inprocess, monkeypatch):
    real = js.JobStore.reconcile

    def reconcile(self):
        signal.raise_signal(signal.SIGTERM)
        return real(self)
    monkeypatch.setattr(js.JobStore, 'reconcile', reconcile)
    assert inprocess.run(lambda: None) == 0
    assert inprocess.lines == [STOPPING]
    assert not rig.socket.exists()


# submit and show end to end (DEC-51; §2).

ALPHA = '1' * 32


def submission(urls, *, preset='video', lane=None, source=None, revision=None, user_agent=UA, **extra):
    value = dict(preset=preset, lane=lane, source=source, revision=revision, user_agent=user_agent, urls=urls)
    value.update(extra)
    return value


def submit(rig, value, **kwargs):
    stdin = value if isinstance(value, bytes) else json.dumps(value).encode()
    return rig.client('submit', stdin=stdin, **kwargs)


def show(rig, job_id):
    result = rig.client('show', str(job_id))
    assert result.stderr == b''
    return result


def shown(rig, job_id):
    result = show(rig, job_id)
    assert result.returncode == 0
    return json.loads(result.stdout)['job']


def settled(rig, job_id, states=('succeeded', 'failed', 'cancelled', 'interrupted')):
    wait_for(lambda: shown(rig, job_id)['state'] in states)
    return shown(rig, job_id)


def test_an_immediate_submission_runs_and_show_prints_its_record_byte_for_byte(rig):
    serve = rig.start().listening()
    result = submit(rig, submission(['output'], preset='gallery', source='alpha', revision=ALPHA,
                                    user_agent='Mozilla/5.0 café'))
    assert (result.returncode, result.stderr) == (0, b'')
    assert result.stdout == b'{"version":1,"submission":1,"created":[{"id":1,"url":"output"}],"duplicates":[]}\n'
    record = settled(rig, 1)
    out = show(rig, 1).stdout
    assert out == (
        '{"version":1,"job":{"id":1,"submission":1,"preset":"gallery","url":"output","lane":null,'
        f'"source":"alpha","revision":"{ALPHA}","user_agent":"Mozilla/5.0 caf\\u00e9","state":"succeeded",'
        '"wait":null,"attempt":1,"cancel_requested":false,'
        f'"created_at":"{record["created_at"]}","queued_at":"{record["queued_at"]}",'
        f'"started_at":"{record["started_at"]}","finished_at":"{record["finished_at"]}",'
        '"exit_status":0,"error":null}}\n').encode()
    assert list(record) == list(js.RECORD_FIELDS)
    log = rig.state / 'jobs' / 'logs' / '1-1.log'
    assert log.read_bytes() == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    assert rig.record('output')['argv'][1:] == [
        'download', '--cookies', str(rig.cookies), '--state', str(rig.state), '--source', 'alpha',
        '--revision', ALPHA, '--user-agent', 'Mozilla/5.0 café', 'gallery', 'output']
    assert serve.stop() == 0
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]


def test_a_lane_submission_runs_its_urls_in_order(rig):
    serve = rig.start().listening()
    result = submit(rig, submission(['wait=a', 'exit=0,n=2'], lane='video'))
    assert json.loads(result.stdout) == {'version': 1, 'submission': 1, 'created': [
        {'id': 1, 'url': 'wait=a'}, {'id': 2, 'url': 'exit=0,n=2'}], 'duplicates': []}
    rig.record('wait=a')
    time.sleep(1.2)  # passes run, but the lane runs one job at a time
    assert [r['url'] for r in rig.records()] == ['wait=a']
    assert rig.client('status').stdout == b'{"version":1,"running":1,"queued":1}\n'
    rig.release('a')
    first, second = settled(rig, 1), settled(rig, 2)
    assert (first['state'], second['state']) == ('succeeded', 'succeeded')
    assert second['started_at'] >= first['finished_at']
    assert serve.stop() == 0


def test_duplicates_are_reported_as_the_store_reports_them(rig):
    serve = rig.start().listening()
    first = submit(rig, submission(['wait=a', 'wait=a', 'exit=0,n=1', 'wait=a'], lane='video'))
    assert first.stdout == (b'{"version":1,"submission":1,"created":[{"id":1,"url":"wait=a"},'
                            b'{"id":2,"url":"exit=0,n=1"}],"duplicates":[]}\n')
    rig.record('wait=a')
    again = submit(rig, submission(['exit=0,n=3', 'wait=a', 'exit=0,n=1']))
    assert again.stdout == (b'{"version":1,"submission":2,"created":[{"id":3,"url":"exit=0,n=3"}],'
                            b'"duplicates":[{"id":1,"url":"wait=a"},{"id":2,"url":"exit=0,n=1"}]}\n')
    other_preset = submit(rig, submission(['wait=a'], preset='gallery', lane='gallery'))
    assert json.loads(other_preset.stdout)['created'] == [{'id': 4, 'url': 'wait=a'}]
    rig.release()
    assert serve.stop() == 0


def test_a_failing_runner_fails_its_job(rig):
    serve = rig.start().listening()
    submit(rig, submission(['exit=3']))
    record = settled(rig, 1)
    assert (record['state'], record['exit_status'], record['error']) == ('failed', 3, None)
    assert serve.stop() == 0


def test_a_restart_interrupts_the_running_job_and_its_lane_continues(rig):
    """D2: SIGKILL the worker mid-job; the next start reconciles and the lane runs on."""
    serve = rig.start().listening()
    submit(rig, submission(['wait=a', 'exit=0,n=2'], lane='video'))
    fixture = rig.record('wait=a')
    serve.process.kill()
    serve.process.wait(30)
    os.kill(fixture['pid'], signal.SIGKILL)
    assert rig.socket.exists()  # a killed worker leaves its socket behind
    again = rig.start().listening()
    assert again.lines() == ['downloadinux-worker: marked 1 running jobs interrupted (worker restarted)',
                             LISTENING.format(rig.socket)]
    interrupted = shown(rig, 1)
    assert (interrupted['state'], interrupted['error'], interrupted['exit_status']) == (
        'interrupted', 'worker restarted', None)
    second = settled(rig, 2)
    assert second['state'] == 'succeeded'
    assert second['started_at'] >= interrupted['finished_at']
    assert again.stop() == 0


SUBMIT_USAGE = b'downloadinux: error: submit: standard input must be one JSON object\n'


@pytest.mark.parametrize('stdin', [
    b'',
    b'   ',
    b'\xff{}',
    b'{"preset":"\xc3"}',
    b'[]',
    b'null',
    b'"text"',
    b'{"a":1,"a":2}',
    b'{"a":NaN}',
    b'{"a":-Infinity}',
    b'{"a":1e999}',
    b'{} {}',
    b'{}\n{}',
    b'\xef\xbb\xbf{}',
    b'{"a":' + b'[' * 100000 + b']' * 100000 + b'}',
    b' ' * worker.LIMIT + b'{}',
], ids=range(16))
def test_submit_refuses_bad_standard_input_without_contacting_the_socket(rig, stdin):
    result = submit(rig, stdin)  # no listener: contacting the socket would exit 3
    assert (result.returncode, result.stdout, result.stderr) == (2, b'', SUBMIT_USAGE)


def test_submit_reads_exactly_the_limit(rig, capsys, monkeypatch):
    """A 32 MiB object is read and sent; the client never examines its members."""
    value = b'{"pad":"' + b'x' * (worker.LIMIT - 10) + b'"}'
    assert len(value) == worker.LIMIT
    sent = []
    monkeypatch.setattr(worker, 'request', lambda path, op, args, timeout: sent.append((op, args)) or 0)

    class Stdin:
        buffer = __import__('io').BytesIO(value)
    monkeypatch.setattr(sys, 'stdin', Stdin)
    assert worker.run(['submit'], socket_path=rig.socket) == 0
    assert sent[0][0] == 'submit' and len(sent[0][1]['pad']) == worker.LIMIT - 10


def test_submit_with_no_standard_input_is_a_usage_error(rig, capsys, monkeypatch):
    monkeypatch.setattr(sys, 'stdin', None)
    assert worker.run(['submit'], socket_path=rig.socket) == 2
    assert capsys.readouterr() == ('', SUBMIT_USAGE.decode())


def test_submit_refuses_an_operand(rig):
    result = rig.client('submit', '-', stdin=b'{}')
    assert (result.returncode, result.stdout, result.stderr) == (
        2, b'', b"downloadinux: error: submit: unexpected operand '-'\n")


@pytest.mark.parametrize('value, reason', [
    (submission([]), 'submission URLs must be a nonempty ordered sequence'),
    (submission(['https://a.invalid/\r']), 'submission URLs must be nonempty strings without CR, LF, or NUL'),
    (submission(['https://a.invalid/\n']), 'submission URLs must be nonempty strings without CR, LF, or NUL'),
    (submission(['https://a.invalid/\0']), 'submission URLs must be nonempty strings without CR, LF, or NUL'),
    (submission(['']), 'submission URLs must be nonempty strings without CR, LF, or NUL'),
    (submission(['x'], preset='audio'), 'submission preset must be video or gallery'),
    (submission(['x'], lane='gallery'), 'submission lane must be null or its preset'),
    (submission(['x'], source='alpha'), 'cookie selection requires both valid source and revision identifiers'),
    (submission(['x'], revision=ALPHA), 'cookie selection requires both valid source and revision identifiers'),
    (submission(['x'], source='Bad Name', revision=ALPHA),
     'cookie selection requires both valid source and revision identifiers'),
    (submission(['x'], user_agent='a\nb'), 'user-agent must be a string without CR, LF, or NUL'),
])
def test_store_refusals_reach_the_client_as_error_replies(rig, serving, value, reason):
    result = submit(rig, value)
    assert (result.returncode, result.stderr) == (1, b'')
    assert result.stdout == worker.encode({'version': 1, 'error': reason})
    # Nothing was allocated: the next valid submission is the first.
    ok = submit(rig, submission(['exit=0,ok']))
    assert json.loads(ok.stdout)['submission'] == 1


@pytest.mark.parametrize('value', [
    {key: value for key, value in submission(['x']).items() if key != 'lane'},
    submission(['x'], extra=1),
    submission('x'),
    submission([1]),
    submission([None]),
    submission([['x']]),
    submission(['x'], preset=None),
    submission(['x'], user_agent=None),
    submission(['x'], lane=0),
    submission(['x'], source=False, revision=False),
    submission(['x'], revision=1),
    submission({'0': 'x'}),
    {},
])
def test_submit_args_of_the_wrong_shape_are_invalid_requests(rig, serving, value):
    result = submit(rig, value)
    assert (result.returncode, result.stdout, result.stderr) == (1, INVALID, b'')
    assert rig.client('status').stdout == EMPTY_STATUS


SWEEP = [chr(code) for code in range(0x20) if code not in (0x00, 0x0a, 0x0d)] + ['\x7f', 'é', ' ',
                                                                                 '﻿', '\U0001f600']


@pytest.mark.parametrize('ensure_ascii', [True, False])
def test_urls_and_user_agents_round_trip_through_submit_store_and_show(rig, ensure_ascii):
    """Every C0 control the store allows, DEL, non-ASCII, and astral, swept (DEC-51)."""
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()  # no nordvpn on PATH: jobs wait, none runs
    urls = [f'https://example.invalid/{char}/{ord(char)}' for char in SWEEP]
    agents = [f'UA {char} {ord(char)}' for char in SWEEP] + [''.join(SWEEP), '-starts-with-a-dash']
    stdin = json.dumps(submission(urls, user_agent=agents[0]), ensure_ascii=ensure_ascii).encode()
    result = submit(rig, stdin)
    assert result.returncode == 0, result
    assert max(result.stdout) < 0x80
    created = json.loads(result.stdout)['created']
    assert [job['url'] for job in created] == urls
    for index, agent in enumerate(agents[1:], 1):
        one = submit(rig, json.dumps(submission([f'ua-{index}'], user_agent=agent), ensure_ascii=ensure_ascii).encode())
        created.append(json.loads(one.stdout)['created'][0])
    expected_agents = [agents[0]] * len(urls) + agents[1:]
    for job, agent in zip(created, expected_agents):
        out = show(rig, job['id']).stdout
        assert max(out) < 0x80
        record = json.loads(out)['job']
        assert (record['url'], record['user_agent']) == (job['url'], agent)
        assert out == worker.encode({'version': 1, 'job': record})
    assert serve.stop() == 0
    logged = serve.out.read_bytes() + serve.err.read_bytes()
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    for value in urls + agents:
        assert value.encode('utf-8', 'surrogatepass') not in logged
    with rig.open() as store:
        assert [(r.url, r.user_agent) for r in store.list()] == [
            (job['url'], agent) for job, agent in zip(created, expected_agents)]


@pytest.mark.parametrize('stdin', [
    b'{"preset":"video","lane":null,"source":null,"revision":null,"user_agent":"UA","urls":["https://a.invalid/\\ud800"]}',
    b'{"preset":"video","lane":null,"source":null,"revision":null,"user_agent":"UA \\udfff","urls":["x"]}',
    b'{"preset":"video","lane":null,"source":null,"revision":null,"user_agent":"UA","urls":["x"],"\\udc80":1}',
    b'{"preset":"video","lane":null,"source":null,"revision":null,"user_agent":"UA","urls":["\\ud83d x"]}',
])
def test_a_lone_surrogate_is_an_invalid_request_and_creates_nothing(rig, serving, stdin):
    result = submit(rig, stdin)
    assert (result.returncode, result.stdout, result.stderr) == (1, INVALID, b'')
    ok = submit(rig, submission(['exit=0,ok']))
    assert json.loads(ok.stdout)['submission'] == 1


@pytest.mark.parametrize('argv, reason', [
    (['show'], 'show: exactly one job ID is required'),
    (['show', '1', '2'], 'show: exactly one job ID is required'),
    (['show', ''], 'show: job ID must be a positive integer'),
    (['show', '0'], 'show: job ID must be a positive integer'),
    (['show', '01'], 'show: job ID must be a positive integer'),
    (['show', '-1'], 'show: job ID must be a positive integer'),
    (['show', '+1'], 'show: job ID must be a positive integer'),
    (['show', '1.0'], 'show: job ID must be a positive integer'),
    (['show', ' 1'], 'show: job ID must be a positive integer'),
    (['show', '1_0'], 'show: job ID must be a positive integer'),
    (['show', '١'], 'show: job ID must be a positive integer'),
    (['show', '１'], 'show: job ID must be a positive integer'),
    (['show', 'abc'], 'show: job ID must be a positive integer'),
    (['show', '9223372036854775808'], 'show: job ID must be a positive integer'),
    (['show', '1' * 5000], 'show: job ID must be a positive integer'),
])
def test_show_usage_errors(rig, capsys, argv, reason):
    assert worker.run(argv, socket_path=rig.socket) == 2
    assert capsys.readouterr() == ('', f'downloadinux: error: {reason}\n')


@pytest.mark.parametrize('job_id', [1, 9223372036854775807])
def test_show_of_an_unknown_id_is_an_error_reply(rig, serving, job_id):
    result = show(rig, job_id)
    assert result.returncode == 1
    assert result.stdout == f'{{"version":1,"error":"unknown job ID {job_id}"}}\n'.encode()


@pytest.mark.parametrize('args', [{}, {'id': 0}, {'id': -1}, {'id': 2 ** 63}, {'id': True}, {'id': '1'},
                                  {'id': 1.0}, {'id': None}, {'id': 1, 'x': 1}, {'ID': 1}])
def test_show_args_of_the_wrong_shape_are_invalid_requests(rig, serving, args):
    assert ask(rig.socket, request_bytes('show', args)) == INVALID
