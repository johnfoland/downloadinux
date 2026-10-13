"""The worker service and its clients (target design, Persistent worker and jobs §1; DEC-50, DEC-51, DEC-54).

Real stores, real fixture runners, and real `serve` processes taking real
signals. Unix socket paths are short (104 bytes on macOS), so every socket
lives under a directory made in `/tmp`, never under pytest's `tmp_path`.

Timeouts: the client's 30-second bound is exercised through `request`'s
`timeout` keyword (a fake listener that never replies, with a short
timeout); the worker's 10-second request read has no seam (DEC-50 names
none), so the stalled-client test waits the real 10 seconds.
"""
import base64
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import random
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
# SIGTERM, whether FIXTURE_SOCKET still existed (`<pid>.socket`), and with
# `drip=N` writes N lines to standard output a tenth of a second apart.
FIXTURE = EXECUTION_FIXTURE.replace(
    '    received.append(number)\n',
    "    with open(base + '.socket', 'a') as handle:\n"
    "        handle.write('%d\\n' % os.path.exists(os.environ.get('FIXTURE_SOCKET', '')))\n"
    '    received.append(number)\n', 1).replace(
    "if 'wait' in words:\n",
    "if 'drip' in words:\n"
    "    for index in range(int(words['drip'])):\n"
    "        os.write(1, b'line %d\\n' % index)\n"
    "        time.sleep(0.1)\n"
    "if 'wait' in words:\n", 1)
assert FIXTURE.count("'drip'") == 2 and FIXTURE.count("'.socket'") == 1

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
        self.clients = []
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

    def spawn(self, *args, stdin=subprocess.PIPE, stdout=None):
        """A client process whose standard input stays open (a pipe) unless given otherwise.

        Standard output (unless given) and error go to files, read by `finish`.
        """
        self.count += 1
        out, err = self.tmp / f'client-{self.count}.out', self.tmp / f'client-{self.count}.err'
        with open(out, 'wb') as out_file, open(err, 'wb') as err_file:
            process = subprocess.Popen(
                [sys.executable, '-I', '-c', LAUNCHER, str(SRC), str(self.socket), str(self.downloads), *args],
                stdin=stdin, stdout=out_file if stdout is None else stdout, stderr=err_file, env=self.env())
        process.out, process.err = out, err
        self.clients.append(process)
        return process

    @staticmethod
    def finish(process, timeout=30):
        """Wait for a spawned client, leaving its standard input as it is; (status, stdout, stderr)."""
        status_code = process.wait(timeout)
        return status_code, process.out.read_bytes(), process.err.read_bytes()

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
        if not signals.exists() or not sockets.exists():
            return []  # the fixture writes `.signals`, then `.socket`: wait for both
        signals, sockets = signals.read_text().split(), sockets.read_text().split()
        if len(signals) != len(sockets):
            return []
        return list(zip(map(int, signals), map(int, sockets)))

    def release(self, name='all'):
        (self.log / f'release-{name}').touch()

    def close(self):
        self.release()
        for process in self.clients:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin is not None:
                process.stdin.close()
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
    ([], 'a subcommand is required: serve, status, submit, show, list, cancel, retry, or logs'),
    (['nope'], "unknown subcommand 'nope'; use serve, status, submit, show, list, cancel, retry, or logs"),
    (['--state'], "unknown subcommand '--state'; use serve, status, submit, show, list, cancel, retry, or logs"),
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


# A stop during an exchange (DEC-52, issue #81).

def test_a_stop_drops_a_client_that_sends_nothing(rig):
    serve = rig.start().listening()
    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stalled.connect(str(rig.socket))
    time.sleep(1.5)  # the worker accepts within one tick and then waits on the request
    started = time.monotonic()
    serve.process.send_signal(signal.SIGTERM)
    assert serve.process.wait(10) == 0
    elapsed = time.monotonic() - started
    stalled.settimeout(5)
    assert stalled.recv(100) == b''  # closed, no reply
    stalled.close()
    assert elapsed < 2
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    assert serve.stderr() == ''


def test_a_stop_drops_a_client_that_never_reads_its_reply_and_the_submission_stays(rig):
    serve = rig.start().listening()
    urls = [f'wait=never,n={index},' + 'x' * 300 for index in range(10000)]
    payload = request_bytes('submit', submission(urls))
    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stalled.connect(str(rig.socket))
    stalled.sendall(payload)
    stalled.shutdown(socket.SHUT_WR)
    # The reply has begun to arrive, far larger than the socket buffers, and
    # is never read: peeking leaves it all in the buffer.
    stalled.settimeout(60)
    assert stalled.recv(1, socket.MSG_PEEK) == b'{'
    started = time.monotonic()
    serve.process.send_signal(signal.SIGTERM)
    assert serve.process.wait(10) == 0
    elapsed = time.monotonic() - started
    stalled.close()
    assert elapsed < 2
    assert serve.lines() == [LISTENING.format(rig.socket), STOPPING]
    assert serve.stderr() == ''
    with rig.open() as store:  # the commit stays
        jobs = store.list()
    assert [job.url for job in jobs] == urls
    assert {job.state for job in jobs} <= {'queued', 'interrupted'}


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


# Usage for the clients that read standard input (DEC-54 *Usage*).

@pytest.mark.parametrize('command', ['list', 'retry'])
@pytest.mark.parametrize('argv, stdin, reason', [
    (['extra'], b'{}', "{}: unexpected operand 'extra'"),
    (['-'], b'{}', "{}: unexpected operand '-'"),
    ([], b'', '{}: standard input must be one JSON object'),
    ([], b'[]', '{}: standard input must be one JSON object'),
    ([], b'{"a":1,"a":2}', '{}: standard input must be one JSON object'),
    ([], b'{"a":NaN}', '{}: standard input must be one JSON object'),
    ([], b'\xff{}', '{}: standard input must be one JSON object'),
    ([], b'{} {}', '{}: standard input must be one JSON object'),
    ([], b' ' * worker.LIMIT + b'{}', '{}: standard input must be one JSON object'),
], ids=range(9))
def test_list_and_retry_usage_errors_contact_no_socket(rig, command, argv, stdin, reason):
    result = rig.client(command, *argv, stdin=stdin)  # no listener: contacting the socket would exit 3
    assert (result.returncode, result.stdout) == (2, b'')
    assert result.stderr == f'downloadinux: error: {reason.format(command)}\n'.encode()


@pytest.mark.parametrize('command', ['list', 'retry'])
def test_list_and_retry_pass_their_object_on_unexamined(rig, monkeypatch, command):
    sent = []
    monkeypatch.setattr(worker, 'request', lambda path, op, args, timeout: sent.append((op, args)) or 0)

    class Stdin:
        buffer = __import__('io').BytesIO(b'{"anything":[1,{"x":null}]}')
    monkeypatch.setattr(sys, 'stdin', Stdin)
    assert worker.run([command], socket_path=rig.socket) == 0
    assert sent == [(command, {'anything': [1, {'x': None}]})]


# list (DEC-54).

def listing(rig, ids=None, states=None, preset=None):
    return rig.client('list', stdin=json.dumps({'ids': ids, 'states': states, 'preset': preset}).encode())


def listed(rig, **filters):
    result = listing(rig, **filters)
    assert (result.returncode, result.stderr) == (0, b'')
    reply = json.loads(result.stdout)
    assert list(reply) == ['version', 'jobs']
    return [job['id'] for job in reply['jobs']]


def prepare_states(rig):
    """Jobs 1-7 in every state a stopped store can hold once the worker reconciles it.

    1 video queued (lane), 2 video succeeded, 3 gallery failed, 4 gallery
    cancelled, 5 video interrupted at start (running now), 6 gallery
    queued, 7 video queued (immediate).
    """
    with rig.open() as store:
        store.submit('video', ['q1'], lane='video', user_agent=UA)
        for url, preset, status in (('s2', 'video', 0), ('f3', 'gallery', 3)):
            [job] = store.submit(preset, [url], user_agent=UA).created
            store.start(job.id)
            store.end(job.id, exit_status=status)
        [job] = store.submit('gallery', ['c4'], user_agent=UA).created
        store.cancel([job.id])
        [job] = store.submit('video', ['i5'], user_agent=UA).created
        store.start(job.id)
        store.submit('gallery', ['q6'], lane='gallery', user_agent=UA)
        store.submit('video', ['q7'], user_agent=UA)


@pytest.fixture
def stable(rig):
    """A worker over `prepare_states`'s jobs that starts nothing: the VPN gate never opens."""
    prepare_states(rig)
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    # The first pass sets each queue head's VPN wait; after it, nothing changes.
    wait_for(lambda: all(shown(rig, job_id)['wait'] == {'reason': 'vpn'} for job_id in (1, 6, 7)))
    yield serve
    assert serve.stop() == 0
    assert serve.stderr() == ''


def test_list_with_no_filter_gives_every_record_as_show_does_in_id_order(rig, stable):
    result = listing(rig)
    assert (result.returncode, result.stderr) == (0, b'')
    jobs = [shown(rig, job_id) for job_id in range(1, 8)]
    assert result.stdout == worker.encode({'version': 1, 'jobs': jobs})
    assert [job['state'] for job in jobs] == ['queued', 'succeeded', 'failed', 'cancelled', 'interrupted',
                                               'queued', 'queued']


@pytest.mark.parametrize('filters, ids', [
    ({'ids': [6, 2, 6, 99, 2]}, [2, 6]),
    ({'ids': [9223372036854775807]}, []),
    ({'states': ['queued']}, [1, 6, 7]),
    ({'states': ['failed', 'interrupted', 'failed', 'cancelled']}, [3, 4, 5]),
    ({'states': ['running']}, []),
    ({'preset': 'gallery'}, [3, 4, 6]),
    ({'preset': 'video'}, [1, 2, 5, 7]),
    ({'ids': [7, 1, 2, 3, 6], 'states': ['queued', 'succeeded'], 'preset': 'video'}, [1, 2, 7]),
    ({'ids': [1], 'states': ['failed']}, []),
])
def test_list_filters_are_anded_and_repeats_change_nothing(rig, stable, filters, ids):
    assert listed(rig, **filters) == ids


LIST_SHAPES = [
    {'ids': None, 'states': None},
    {'ids': None, 'states': None, 'preset': None, 'extra': None},
    {'ids': [], 'states': None, 'preset': None},
    {'ids': None, 'states': [], 'preset': None},
    {'ids': [True], 'states': None, 'preset': None},
    {'ids': [0], 'states': None, 'preset': None},
    {'ids': [-1], 'states': None, 'preset': None},
    {'ids': [2 ** 63], 'states': None, 'preset': None},
    {'ids': [1.0], 'states': None, 'preset': None},
    {'ids': ['1'], 'states': None, 'preset': None},
    {'ids': 1, 'states': None, 'preset': None},
    {'ids': [1, None], 'states': None, 'preset': None},
    {'ids': None, 'states': ['Queued'], 'preset': None},
    {'ids': None, 'states': ['queued', 'paused'], 'preset': None},
    {'ids': None, 'states': 'queued', 'preset': None},
    {'ids': None, 'states': [None], 'preset': None},
    {'ids': None, 'states': None, 'preset': 'audio'},
    {'ids': None, 'states': None, 'preset': 'Video'},
    {'ids': None, 'states': None, 'preset': ''},
    {'ids': None, 'states': None, 'preset': ['video']},
    {'ids': None, 'states': None, 'preset': True},
]


@pytest.mark.parametrize('args', LIST_SHAPES, ids=range(len(LIST_SHAPES)))
def test_list_args_of_the_wrong_shape_are_invalid_requests(rig, serving, args):
    assert ask(rig.socket, request_bytes('list', args)) == INVALID


def test_a_listing_over_the_limit_is_refused_and_one_at_it_is_sent(rig, inprocess, monkeypatch):
    with rig.open() as store:
        store.submit('video', [f'https://example.invalid/{index}/' + 'x' * 1000 for index in range(12)],
                     user_agent=UA)
    every = request_bytes('list', {'ids': None, 'states': None, 'preset': None})

    def drive():
        inprocess.wait_line(LISTENING.format(rig.socket))
        wait_for(lambda: b'"wait":{"reason":"vpn"}' in ask(rig.socket, every))  # the head's wait is set: stable now
        reply = ask(rig.socket, every)
        assert reply.startswith(b'{"version":1,"jobs":[{"id":1,')
        monkeypatch.setattr(worker, 'LIMIT', len(reply))  # exactly at the limit: sent
        assert ask(rig.socket, every) == reply
        monkeypatch.setattr(worker, 'LIMIT', len(reply) - 1)  # one byte over: refused
        assert ask(rig.socket, every) == b'{"version":1,"error":"the job listing would exceed 32 MiB; narrow it"}\n'
        narrow = json.loads(ask(rig.socket, request_bytes('list', {'ids': [12], 'states': None, 'preset': None})))
        assert [job['id'] for job in narrow['jobs']] == [12]

    assert inprocess.run(drive, environ={'DL_VPN': 'on'}) == 0


# Submission size (DEC-54 *Submission size*, issue #79).

TOO_LARGE = b'{"version":1,"error":"submission too large: its reply would exceed 32 MiB"}\n'


def test_the_submit_bound_counts_distinct_urls_by_their_escaped_length():
    assert worker._submit_bound(['a']) == 128 + 34 + 3
    assert worker._submit_bound(['a', 'a', 'b', 'a']) == 128 + 2 * (34 + 3)
    assert worker._submit_bound(['é']) == 128 + 34 + len('"\\u00e9"')
    assert worker._submit_bound(['\U0001f600']) == 128 + 34 + len('"\\ud83d\\ude00"')
    assert worker._submit_bound(['"\\\x01']) == 128 + 34 + len('"\\"\\\\\\u0001"')
    assert worker._submit_bound(['é', 'e\u0301']) == 128 + 2 * 34 + 8 + 9  # no normalization


def sized(target):
    """URLs, with repeats and non-ASCII, whose submit bound is exactly `target`."""
    urls = ['https://example.invalid/é', 'https://example.invalid/\U0001f600', 'https://example.invalid/é',
            'https://example.invalid/a"\\b', 'https://example.invalid/é']
    filler = target - worker._submit_bound(urls) - 34 - 2
    urls.append('https://example.invalid/' + 'f' * (filler - len('https://example.invalid/')))
    assert worker._submit_bound(urls) == target
    return urls


def test_a_submission_whose_reply_could_exceed_the_limit_is_refused_and_commits_nothing(rig, inprocess, monkeypatch):
    monkeypatch.setattr(worker, 'LIMIT', 20000)
    over = request_bytes('submit', submission(sized(20001), user_agent='UA'))
    assert len(over) < 20000

    def drive():
        inprocess.wait_line(LISTENING.format(rig.socket))
        assert ask(rig.socket, over) == TOO_LARGE
        assert status(rig.socket) == {'version': 1, 'running': 0, 'queued': 0}

    assert inprocess.run(drive, environ={'DL_VPN': 'on'}) == 0
    with rig.open() as store:
        assert store.list() == []
        assert store.submit('video', ['x'], user_agent=UA).submission == 1  # no submission was allocated


def test_a_submission_at_the_bound_is_accepted_and_its_reply_fits(rig, inprocess, monkeypatch):
    monkeypatch.setattr(worker, 'LIMIT', 20000)
    urls = sized(20000)

    def drive():
        inprocess.wait_line(LISTENING.format(rig.socket))
        reply = ask(rig.socket, request_bytes('submit', submission(urls, user_agent='UA')))
        assert len(reply) <= worker._submit_bound(urls)
        created = json.loads(reply)['created']
        assert [job['url'] for job in created] == list(dict.fromkeys(urls))

    assert inprocess.run(drive, environ={'DL_VPN': 'on'}) == 0


URL_CHARACTERS = SWEEP + ['"', '\\', '/', 'a', 'Z', '0', '\u2028', '\U0010ffff', '\ufffd']


def random_urls(chooser):
    pool = [''.join(chooser.choice(URL_CHARACTERS) for _ in range(chooser.randint(1, 40)))
            for _ in range(chooser.randint(1, 30))]
    return [chooser.choice(pool) for _ in range(chooser.randint(1, 60))]


def test_no_submit_reply_exceeds_its_bound_even_with_the_largest_ids():
    """Holds the formula to the encoder: the widest IDs and every URL a created or duplicate entry."""
    chooser = random.Random(79)
    for _ in range(500):
        urls = random_urls(chooser)
        distinct = list(dict.fromkeys(urls))
        split = chooser.randint(0, len(distinct))
        reply = {'version': 1, 'submission': worker.MAX_ID,
                 'created': [{'id': worker.MAX_ID, 'url': url} for url in distinct[:split]],
                 'duplicates': [{'id': worker.MAX_ID, 'url': url} for url in distinct[split:]]}
        assert len(worker.encode(reply)) <= worker._submit_bound(urls)


def test_real_submit_replies_stay_within_their_bound(rig):
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()  # nothing runs: later rounds see duplicates
    chooser = random.Random(64)
    for _ in range(20):
        urls = random_urls(chooser)
        result = submit(rig, submission(urls))
        assert result.returncode == 0, result
        assert len(result.stdout) <= worker._submit_bound(urls)
    assert serve.stop() == 0


# cancel (DEC-54; §5 *Cancel*, DEC-48).

def cancelled(rig, *ids):
    result = rig.client('cancel', *map(str, ids))
    assert (result.returncode, result.stderr) == (0, b''), result
    reply = json.loads(result.stdout)
    assert result.stdout == worker.encode(reply) and list(reply) == ['version', 'outcomes']
    return [tuple(outcome.values()) for outcome in reply['outcomes']]


@pytest.mark.parametrize('argv, reason', [
    (['cancel'], 'cancel: at least one job ID is required'),
    (['cancel', '0'], 'cancel: job ID must be a positive integer'),
    (['cancel', '1', '01'], 'cancel: job ID must be a positive integer'),
    (['cancel', '1', '-2'], 'cancel: job ID must be a positive integer'),
    (['cancel', '--', '1'], 'cancel: job ID must be a positive integer'),
    (['cancel', '1', ''], 'cancel: job ID must be a positive integer'),
    (['cancel', '9223372036854775808'], 'cancel: job ID must be a positive integer'),
    (['cancel', '١'], 'cancel: job ID must be a positive integer'),
])
def test_cancel_usage_errors(rig, capsys, argv, reason):
    assert worker.run(argv, socket_path=rig.socket) == 2  # no listener: contacting it would exit 3
    assert capsys.readouterr() == ('', f'downloadinux: error: {reason}\n')


def test_cancel_sends_its_ids_in_order_with_repeats(rig, monkeypatch):
    sent = []
    monkeypatch.setattr(worker, 'request', lambda path, op, args, timeout: sent.append((op, args)) or 0)
    assert worker.run(['cancel', '3', '1', '3', '9223372036854775807'], socket_path=rig.socket) == 0
    assert sent == [('cancel', {'ids': [3, 1, 3, 9223372036854775807]})]


def test_cancel_ends_queued_jobs_refuses_finished_ones_and_keeps_request_order(rig, stable):
    assert cancelled(rig, 2, 99, 7, 2, 1, 7, 3) == [
        (2, 'refused', 'succeeded'), (99, 'unknown', None), (7, 'cancelled', 'cancelled'),
        (1, 'cancelled', 'cancelled'), (3, 'refused', 'failed')]
    assert (shown(rig, 1)['state'], shown(rig, 1)['cancel_requested']) == ('cancelled', True)
    assert shown(rig, 7)['state'] == 'cancelled'
    assert cancelled(rig, 4, 5) == [(4, 'refused', 'cancelled'), (5, 'refused', 'interrupted')]


def test_cancel_signals_a_running_jobs_runner_and_the_job_ends_cancelled(rig):
    serve = rig.start().listening()
    submit(rig, submission(['wait=a']))
    record = rig.record('wait=a')
    assert cancelled(rig, 1) == [(1, 'requested', 'running')]
    wait_for(lambda: rig.received(record))
    assert rig.received(record) == [(signal.SIGTERM, 1)]  # signalled by the worker, socket still up
    ended = settled(rig, 1)
    assert (ended['state'], ended['cancel_requested'], ended['exit_status']) == (
        'cancelled', True, 128 + signal.SIGTERM)
    assert serve.stop() == 0


def test_a_cancel_signal_failure_is_the_error_reply_after_the_store_commits(rig, inprocess, monkeypatch):
    with rig.open() as store:
        store.submit('video', ['wait=a', 'q2'], lane='video', user_agent=UA)  # q2 waits behind wait=a
    real = we.Executor._signal

    def failing(self, job_ids):
        job_ids = list(job_ids)
        real(self, job_ids)  # the runner still gets its SIGTERM
        return (job_ids[0], PermissionError(errno.EPERM, os.strerror(errno.EPERM))) if job_ids else None
    monkeypatch.setattr(we.Executor, '_signal', failing)

    def drive():
        rig.record('wait=a')
        reply = ask(rig.socket, request_bytes('cancel', {'ids': [1, 2]}))
        assert reply == worker.encode({'version': 1, 'error': (
            f'cannot signal the runner of job 1: {os.strerror(errno.EPERM)}')})
        ended = json.loads(ask(rig.socket, request_bytes('show', {'id': 2})))['job']
        assert ended['state'] == 'cancelled'  # committed before the signals
        wait_for(lambda: json.loads(ask(rig.socket, request_bytes('show', {'id': 1})))['job']['state'] == 'cancelled')

    assert inprocess.run(drive) == 0


def test_a_cancel_whose_reply_could_exceed_the_limit_changes_nothing(rig, inprocess, monkeypatch):
    prepare_states(rig)
    monkeypatch.setattr(worker, 'LIMIT', 128 + 72 * 3)

    def drive():
        inprocess.wait_line(LISTENING.format(rig.socket))
        over = ask(rig.socket, request_bytes('cancel', {'ids': [1, 6, 7, 2]}))
        assert over == worker.encode({'version': 1, 'error': 'too many job IDs: the reply would exceed 32 MiB'})
        assert status(rig.socket)['queued'] == 3
        three = request_bytes('cancel', {'ids': [1, 6, 1, 7, 6]})
        monkeypatch.setattr(worker, 'LIMIT', 128 + 72 * 3 - 1)  # 72 bytes per distinct ID, exactly
        assert b'too many job IDs' in ask(rig.socket, three)
        assert status(rig.socket)['queued'] == 3
        monkeypatch.setattr(worker, 'LIMIT', 128 + 72 * 3)
        at = json.loads(ask(rig.socket, three))
        assert [outcome['outcome'] for outcome in at['outcomes']] == ['cancelled'] * 3

    assert inprocess.run(drive, environ={'DL_VPN': 'on'}) == 0


def test_no_cancel_reply_exceeds_its_bound():
    outcomes = [{'id': worker.MAX_ID, 'outcome': name, 'state': state}
                for name in worker.CANCEL_OUTCOMES.values() for state in (*js.STATES, None)]
    for count in (1, 2, 50):
        reply = {'version': 1, 'outcomes': [max(outcomes, key=lambda o: len(json.dumps(o)))] * count}
        assert len(worker.encode(reply)) <= worker._ids_bound(list(range(count)), 72)


@pytest.mark.parametrize('args', [{}, {'ids': []}, {'ids': [0]}, {'ids': [True]}, {'ids': [2 ** 63]},
                                  {'ids': ['1']}, {'ids': 1}, {'ids': None}, {'ids': [1], 'x': 1}])
def test_cancel_args_of_the_wrong_shape_are_invalid_requests(rig, serving, args):
    assert ask(rig.socket, request_bytes('cancel', args)) == INVALID


# retry (DEC-54; §3 and §5 *Retry*, DEC-46).

def entry(job_id, source=None, revision=None, user_agent=UA, **extra):
    return dict(id=job_id, source=source, revision=revision, user_agent=user_agent, **extra)


def retried(rig, *entries):
    result = rig.client('retry', stdin=json.dumps({'jobs': list(entries)}).encode())
    assert (result.returncode, result.stderr) == (0, b''), result
    reply = json.loads(result.stdout)
    assert result.stdout == worker.encode(reply) and list(reply) == ['version', 'submission', 'outcomes']
    return reply['submission'], [tuple(outcome.values()) for outcome in reply['outcomes']]


def test_retry_requeues_finished_jobs_as_one_submission_with_the_supplied_selection(rig, stable):
    before = shown(rig, 3)
    submission_id, outcomes = retried(rig, entry(3, 'alpha', ALPHA, 'New UA'), entry(5), entry(3))
    assert outcomes == [(3, 'requeued', 'queued', None), (5, 'requeued', 'queued', None)]
    assert submission_id is not None and submission_id > max(shown(rig, job_id)['submission'] for job_id in (1, 6, 7))
    third, fifth = shown(rig, 3), shown(rig, 5)
    assert (third['state'], third['attempt'], third['submission']) == ('queued', 2, submission_id)
    assert (third['source'], third['revision'], third['user_agent']) == ('alpha', ALPHA, 'New UA')
    assert (third['created_at'], third['finished_at'], third['exit_status']) == (before['created_at'], None, None)
    assert (fifth['state'], fifth['attempt'], fifth['submission'], fifth['error']) == ('queued', 2, submission_id, None)
    assert retried(rig, entry(4))[1] == [(4, 'requeued', 'queued', None)]


def test_retry_refuses_unretryable_and_unknown_ids_with_a_null_submission(rig, stable):
    assert retried(rig, entry(2), entry(99), entry(1), entry(2)) == (None, [
        (2, 'refused', 'succeeded', None), (99, 'unknown', None, None), (1, 'refused', 'queued', None)])
    assert shown(rig, 2)['attempt'] == 1


def test_retry_of_a_job_whose_url_is_held_names_its_holder(rig):
    with rig.open() as store:
        [failed] = store.submit('video', ['dup'], user_agent=UA).created
        store.start(failed.id)
        store.end(failed.id, exit_status=1)
        [holder] = store.submit('video', ['dup'], lane='video', user_agent=UA).created
        [other] = store.submit('gallery', ['dup2'], user_agent=UA).created
        store.cancel([other.id])
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    # The second retried job's preset and URL become held by the first once it is requeued.
    assert retried(rig, entry(failed.id), entry(other.id)) == (4, [
        (failed.id, 'shadowed', 'failed', holder.id), (other.id, 'requeued', 'queued', None)])
    assert serve.stop() == 0


@pytest.mark.parametrize('bad, reason', [
    (entry(3, 'alpha', 'not-a-revision'), 'cookie selection requires both valid source and revision identifiers'),
    (entry(3, 'alpha', None), 'cookie selection requires both valid source and revision identifiers'),
    (entry(3, user_agent='a\rb'), 'user-agent must be a string without CR, LF, or NUL'),
])
def test_an_invalid_selection_refuses_the_whole_retry_with_the_stores_message(rig, stable, bad, reason):
    result = rig.client('retry', stdin=json.dumps({'jobs': [entry(5), bad]}).encode())
    assert (result.returncode, result.stderr) == (1, b'')
    assert result.stdout == worker.encode({'version': 1, 'error': reason})
    assert (shown(rig, 3)['state'], shown(rig, 5)['state']) == ('failed', 'interrupted')


RETRY_SHAPES = [
    {},
    {'jobs': []},
    {'jobs': None},
    {'jobs': entry(3)},
    {'jobs': [entry(3)], 'x': 1},
    {'jobs': [{key: value for key, value in entry(3).items() if key != 'user_agent'}]},
    {'jobs': [entry(3, extra=1)]},
    {'jobs': [entry(True)]},
    {'jobs': [entry(0)]},
    {'jobs': [entry(2 ** 63)]},
    {'jobs': [entry('3')]},
    {'jobs': [entry(3, source=1)]},
    {'jobs': [entry(3, revision=False)]},
    {'jobs': [entry(3, user_agent=None)]},
    {'jobs': [entry(3), [3, None, None, 'UA']]},
]


@pytest.mark.parametrize('args', RETRY_SHAPES, ids=range(len(RETRY_SHAPES)))
def test_retry_args_of_the_wrong_shape_are_invalid_requests(rig, stable, args):
    assert ask(rig.socket, request_bytes('retry', args)) == INVALID
    assert shown(rig, 3)['state'] == 'failed'


def test_a_retry_whose_reply_could_exceed_the_limit_changes_nothing(rig, inprocess, monkeypatch):
    prepare_states(rig)
    monkeypatch.setattr(worker, 'LIMIT', 128 + 100 * 2)
    over = request_bytes('retry', {'jobs': [entry(3), entry(4), entry(5)]})
    assert len(over) < worker.LIMIT

    def drive():
        inprocess.wait_line(LISTENING.format(rig.socket))
        assert ask(rig.socket, over) == worker.encode(
            {'version': 1, 'error': 'too many job IDs: the reply would exceed 32 MiB'})
        assert status(rig.socket)['queued'] == 3
        two = request_bytes('retry', {'jobs': [entry(3), entry(4), entry(3)]})
        monkeypatch.setattr(worker, 'LIMIT', 128 + 100 * 2 - 1)  # 100 bytes per distinct ID, exactly
        assert b'too many job IDs' in ask(rig.socket, two)
        assert status(rig.socket)['queued'] == 3
        monkeypatch.setattr(worker, 'LIMIT', 128 + 100 * 2)
        at = json.loads(ask(rig.socket, two))
        assert [outcome['outcome'] for outcome in at['outcomes']] == ['requeued', 'requeued']

    assert inprocess.run(drive, environ={'DL_VPN': 'on'}) == 0


def test_no_retry_reply_exceeds_its_bound():
    widest = {'id': worker.MAX_ID, 'outcome': 'requeued', 'state': 'interrupted', 'holder': worker.MAX_ID}
    for count in (1, 2, 50):
        reply = {'version': 1, 'submission': worker.MAX_ID, 'outcomes': [widest] * count}
        assert len(worker.encode(reply)) <= worker._ids_bound(list(range(count)), 100)


# logs (DEC-54).

LOGS_USAGE = [
    (['logs'], 'logs: exactly one job ID is required'),
    (['logs', '1', '2'], 'logs: exactly one job ID is required'),
    (['logs', '--follow'], 'logs: exactly one job ID is required'),
    (['logs', '0'], 'logs: job ID must be a positive integer'),
    (['logs', '01', '--attempt', 'x'], 'logs: job ID must be a positive integer'),
    (['logs', ''], 'logs: job ID must be a positive integer'),
    (['logs', '1', '--attempt', '0'], 'logs: --attempt must be a positive integer'),
    (['logs', '1', '--attempt', '9223372036854775808'], 'logs: --attempt must be a positive integer'),
    (['logs', '1', '--attempt', '--follow'], 'logs: --attempt must be a positive integer'),  # the next word, whatever it is
    (['logs', '1', '--attempt', '-1'], 'logs: --attempt must be a positive integer'),
    (['logs', '--attempt'], 'logs: --attempt needs a value'),
    (['logs', '1', '--attempt'], 'logs: --attempt needs a value'),
    (['logs', '1', '--attempt', '1', '--attempt'], 'logs: --attempt needs a value'),
    (['logs', '1', '--attempt', '1', '--attempt', '2'], 'logs: --attempt given more than once'),
    (['logs', '--follow', '1', '--follow'], 'logs: --follow given more than once'),
    (['logs', '1', '-f'], "logs: unknown option '-f'"),
    (['logs', '1', '-'], "logs: unknown option '-'"),
    (['logs', '-1'], "logs: unknown option '-1'"),
    (['logs', '--attempt=1', '1'], "logs: unknown option '--attempt=1'"),
    # The first problem met scanning the words in order wins.
    (['logs', '1', '2', '--bogus'], "logs: unknown option '--bogus'"),
    (['logs', 'x', '--follow', '--follow', '--bogus'], 'logs: --follow given more than once'),
    (['logs', '--bogus', '--follow', '--follow'], "logs: unknown option '--bogus'"),
    (['logs', '--attempt', '1', '--attempt', '2', '--bogus'], 'logs: --attempt given more than once'),
    (['logs', 'x', '--attempt', 'y'], 'logs: job ID must be a positive integer'),
]


@pytest.mark.parametrize('argv, reason', LOGS_USAGE, ids=range(len(LOGS_USAGE)))
def test_logs_usage_errors_in_scan_order(rig, capsys, argv, reason):
    assert worker.run(argv, socket_path=rig.socket) == 2  # no listener: contacting it would exit 3
    assert capsys.readouterr() == ('', f'downloadinux: error: {reason}\n')


@pytest.mark.parametrize('argv, parsed', [
    (['1'], (1, None, False)),
    (['--follow', '9223372036854775807'], (9223372036854775807, None, True)),
    (['--attempt', '2', '7', '--follow'], (7, 2, True)),
    (['7', '--follow', '--attempt', '3'], (7, 3, True)),
])
def test_logs_options_come_in_any_order(argv, parsed):
    assert worker.parse_logs(argv) == parsed


def log_file(rig, job_id, attempt):
    return rig.state / 'jobs' / 'logs' / f'{job_id}-{attempt}.log'


def plant_log(rig, job_id, attempt, data):
    path = log_file(rig, job_id, attempt)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def prepare_attempts(rig, *, attempts=2, last=0):
    """Job 1, ended `attempts` times (the last with status `last`): every earlier attempt failed."""
    with rig.open() as store:
        [job] = store.submit('video', ['logged'], user_agent=UA).created
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                store.retry([(job.id, None, None, UA)])
            store.start(job.id)
            store.end(job.id, exit_status=last if attempt == attempts else 1)
    return job.id


def chunks(stdout):
    """Each printed line as a reply, checked to be in the output form."""
    replies = []
    for line in stdout.splitlines(keepends=True):
        reply = json.loads(line)
        assert line == worker.encode(reply)
        assert list(reply) == ['version', 'attempt', 'ended', 'offset', 'data']
        replies.append(reply)
    return replies


def joined(replies):
    return b''.join(base64.b64decode(reply['data'], validate=True) for reply in replies)


def test_a_log_over_1_mib_comes_in_chunks_and_ends_with_one_empty_reply(rig):
    prepare_attempts(rig, attempts=1)
    data = random.Random(54).randbytes(5 * worker.CHUNK // 2)
    plant_log(rig, 1, 1, data)
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    result = rig.client('logs', '1', stdin=b'')
    assert (result.returncode, result.stderr) == (0, b'')
    replies = chunks(result.stdout)
    assert [(r['attempt'], r['ended'], r['offset'], len(base64.b64decode(r['data']))) for r in replies] == [
        (1, True, worker.CHUNK, worker.CHUNK), (1, True, 2 * worker.CHUNK, worker.CHUNK),
        (1, True, len(data), worker.CHUNK // 2), (1, True, len(data), 0)]
    assert replies[-1]['data'] == ''
    assert joined(replies) == data
    assert serve.stop() == 0


def test_every_byte_value_round_trips_through_a_log(rig):
    prepare_attempts(rig, attempts=1)
    data = bytes(range(256)) + b'\0\0 \xff\xfe \xc3( \xed\xa0\x80 \xf4\x90\x80\x80 \r\n' + bytes(range(255, -1, -1))
    plant_log(rig, 1, 1, data)
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    result = rig.client('logs', '1')
    assert (result.returncode, result.stderr) == (0, b'')
    replies = chunks(result.stdout)
    assert len(replies) == 2 and joined(replies) == data
    assert replies[0]['data'] == base64.b64encode(data).decode('ascii')
    assert serve.stop() == 0


def test_attempt_selects_an_earlier_attempts_log(rig):
    prepare_attempts(rig)
    plant_log(rig, 1, 1, b'first\n')
    plant_log(rig, 1, 2, b'second\n')
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    earlier = rig.client('logs', '--attempt', '1', '1')
    assert (earlier.returncode, earlier.stderr) == (0, b'')
    assert earlier.stdout == (b'{"version":1,"attempt":1,"ended":true,"offset":6,"data":"Zmlyc3QK"}\n'
                              b'{"version":1,"attempt":1,"ended":true,"offset":6,"data":""}\n')
    current = rig.client('logs', '1')
    assert [(r['attempt'], r['ended']) for r in chunks(current.stdout)] == [(2, True), (2, True)]
    assert joined(chunks(current.stdout)) == b'second\n'
    assert serve.stop() == 0


@pytest.mark.parametrize('argv, reply', [
    (['logs', '1', '--attempt', '3'], b'{"version":1,"error":"job 1 has no attempt 3"}\n'),
    (['logs', '9'], b'{"version":1,"error":"unknown job ID 9"}\n'),
    (['logs', '9223372036854775807', '--follow'], b'{"version":1,"error":"unknown job ID 9223372036854775807"}\n'),
])
def test_an_error_reply_is_printed_and_exits_1(rig, argv, reply):
    prepare_attempts(rig)
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    process = rig.spawn(*argv)
    assert rig.finish(process) == (1, reply, b'')
    assert serve.stop() == 0


def test_a_queued_retry_with_no_log_gives_one_empty_reply_that_has_not_ended(rig):
    prepare_attempts(rig, last=1)
    with rig.open() as store:
        assert store.retry([(1, None, None, UA)]).outcomes[0].outcome == 'requeued'
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()  # the retry stays queued
    result = rig.client('logs', '1')
    assert (result.returncode, result.stderr) == (0, b'')
    assert result.stdout == b'{"version":1,"attempt":3,"ended":false,"offset":0,"data":""}\n'
    assert not log_file(rig, 1, 3).exists()
    assert serve.stop() == 0


def test_without_follow_standard_input_is_never_read(rig):
    prepare_attempts(rig, attempts=1)
    plant_log(rig, 1, 1, b'text\n')
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    read, write = os.pipe()
    try:
        os.write(write, b'keep')
        code, out, err = rig.finish(rig.spawn('logs', '1', stdin=read))
        assert (code, err) == (0, b'')
        assert joined(chunks(out)) == b'text\n'
        assert os.read(read, 100) == b'keep'  # still there: the client left it alone
    finally:
        os.close(read)
        os.close(write)
    assert serve.stop() == 0


def test_following_a_running_job_prints_every_byte_and_ends_once_it_has_ended(rig):
    serve = rig.start().listening()
    submit(rig, submission(['drip=10']))
    code, out, err = rig.finish(rig.spawn('logs', '1', '--follow'), 60)  # input stays open: only the end stops it
    assert (code, err) == (0, b'')
    replies = chunks(out)
    assert joined(replies) == b''.join(b'line %d\n' % index for index in range(10))
    assert joined(replies) == log_file(rig, 1, 1).read_bytes()
    assert (replies[-1]['data'], replies[-1]['ended']) == ('', True)
    assert all(reply['data'] for reply in replies[:-1])  # waits in between print nothing
    assert len(replies) > 2  # it followed: more than one chunk arrived
    assert serve.stop() == 0


def test_closing_standard_input_detaches_a_follower_within_a_second(rig):
    serve = rig.start().listening()
    submit(rig, submission(['output,wait=a']))
    rig.record('output,wait=a')
    process = rig.spawn('logs', '1', '--follow')
    time.sleep(1.5)  # following: several requests and waits
    started = time.monotonic()
    process.stdin.close()
    code, out, err = rig.finish(process, 10)
    assert time.monotonic() - started < 1
    assert (code, err) == (0, b'')
    assert joined(chunks(out)) == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    assert all(reply['data'] for reply in chunks(out))  # nothing more after the close
    assert shown(rig, 1)['state'] == 'running'
    assert serve.stop() == 0


@pytest.fixture
def stdin_pipe():
    """A real pipe on this process's descriptor 0 for the test; yields its write end."""
    read, write = os.pipe()
    saved = os.dup(0)
    os.dup2(read, 0)
    os.close(read)
    try:
        yield write
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        try:
            os.close(write)
        except OSError:
            pass


def test_the_input_watch_ends_at_end_of_file_during_the_wait_and_discards_data(stdin_pipe):
    started = time.monotonic()
    assert worker._input_ended(0.3) is False  # open and quiet: the whole wait
    assert 0.25 < time.monotonic() - started < 2
    os.write(stdin_pipe, b'ignored\n')
    started = time.monotonic()
    assert worker._input_ended(0.3) is False  # data is read and discarded, not end of file
    assert 0.25 < time.monotonic() - started < 2
    assert worker._input_ended(0) is False  # nothing left over
    timer = threading.Timer(0.2, os.close, (stdin_pipe,))
    timer.start()
    started = time.monotonic()
    try:
        assert worker._input_ended(5) is True  # the close ends the wait at once
    finally:
        timer.join()
    assert time.monotonic() - started < 1
    assert worker._input_ended(0) is True


def test_a_follower_with_dev_null_input_exits_at_once_without_a_request(rig):
    """No worker listens: any request would exit 3."""
    with open(os.devnull, 'rb') as null:
        started = time.monotonic()
        result = rig.finish(rig.spawn('logs', '1', '--follow', stdin=null))
    assert result == (0, b'', b'')
    assert time.monotonic() - started < 5


def test_a_worker_stopped_mid_follow_exits_3_keeping_the_lines_printed(rig):
    serve = rig.start().listening()
    submit(rig, submission(['output,wait=a']))
    rig.record('output,wait=a')
    process = rig.spawn('logs', '1', '--follow')
    wait_for(lambda: log_file(rig, 1, 1).stat().st_size > 0)
    time.sleep(1)  # the follower has printed the output and is waiting for more
    assert serve.stop() == 0
    code, out, err = rig.finish(process)
    assert code == 3
    assert joined(chunks(out)) == b'out-1\nerr-1 \xff\xfe\nout-2 \x80err-2\n'
    assert err.startswith(b'downloadinux: error: the worker is not answering: ')
    assert err.count(b'\n') == 1


def plant_symlink_to_log(path):
    (path.parent / 'target').write_bytes(b'secret')
    path.symlink_to(path.parent / 'target')


def plant_dangling_symlink(path):
    path.symlink_to(path.parent / 'absent')


def plant_fifo(path):
    os.mkfifo(path)


@pytest.mark.parametrize('plant', [plant_symlink_to_log, plant_dangling_symlink, plant_fifo, plant_directory])
def test_anything_but_a_regular_file_at_the_log_name_is_refused(rig, plant):
    prepare_attempts(rig, attempts=1)
    plant(log_file(rig, 1, 1))
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    started = time.monotonic()
    result = rig.client('logs', '1')
    assert time.monotonic() - started < 10
    assert (result.returncode, result.stderr) == (1, b'')
    assert result.stdout == worker.encode({'version': 1, 'error': f'{log_file(rig, 1, 1)}: not a regular file'})
    assert rig.client('status').stdout == b'{"version":1,"running":0,"queued":0}\n'  # never blocked
    assert serve.stop() == 0


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores file permissions')
def test_an_unreadable_log_is_its_os_reason(rig):
    prepare_attempts(rig, attempts=1)
    plant_log(rig, 1, 1, b'text').chmod(0)
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    result = rig.client('logs', '1')
    assert (result.returncode, result.stderr) == (1, b'')
    assert result.stdout == worker.encode({'version': 1, 'error': f'{log_file(rig, 1, 1)}: Permission denied'})
    assert serve.stop() == 0


def test_the_logs_op_reads_from_the_offset_and_never_past_the_largest_id(rig):
    prepare_attempts(rig, attempts=1)
    plant_log(rig, 1, 1, b'0123456789')
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()

    def chunk(offset, attempt=None):
        return json.loads(ask(rig.socket, request_bytes('logs', {'id': 1, 'attempt': attempt, 'offset': offset})))
    assert chunk(4) == {'version': 1, 'attempt': 1, 'ended': True, 'offset': 10, 'data': 'NDU2Nzg5'}
    assert chunk(10, 1) == {'version': 1, 'attempt': 1, 'ended': True, 'offset': 10, 'data': ''}
    assert chunk(11) == {'version': 1, 'attempt': 1, 'ended': True, 'offset': 11, 'data': ''}
    assert chunk(worker.MAX_ID) == {'version': 1, 'attempt': 1, 'ended': True, 'offset': worker.MAX_ID, 'data': ''}
    assert serve.stop() == 0


LOGS_SHAPES = [
    {'id': 1, 'attempt': None},
    {'id': 1, 'attempt': None, 'offset': 0, 'x': 0},
    {'id': 0, 'attempt': None, 'offset': 0},
    {'id': True, 'attempt': None, 'offset': 0},
    {'id': 2 ** 63, 'attempt': None, 'offset': 0},
    {'id': 1, 'attempt': 0, 'offset': 0},
    {'id': 1, 'attempt': True, 'offset': 0},
    {'id': 1, 'attempt': 2 ** 63, 'offset': 0},
    {'id': 1, 'attempt': '1', 'offset': 0},
    {'id': 1, 'attempt': None, 'offset': -1},
    {'id': 1, 'attempt': None, 'offset': 2 ** 63},
    {'id': 1, 'attempt': None, 'offset': False},
    {'id': 1, 'attempt': None, 'offset': None},
    {'id': 1, 'attempt': None, 'offset': 0.0},
]


@pytest.mark.parametrize('args', LOGS_SHAPES, ids=range(len(LOGS_SHAPES)))
def test_logs_args_of_the_wrong_shape_are_invalid_requests(rig, serving, args):
    assert ask(rig.socket, request_bytes('logs', args)) == INVALID


@pytest.mark.parametrize('reply', [
    b'{"version":1,"attempt":1,"ended":true,"offset":0}\n',
    b'{"version":1,"attempt":1,"ended":1,"offset":0,"data":""}\n',
    b'{"version":1,"attempt":0,"ended":true,"offset":0,"data":""}\n',
    b'{"version":1,"attempt":1,"ended":true,"offset":-1,"data":""}\n',
    b'{"version":1,"attempt":1,"ended":true,"offset":0,"data":null}\n',
    b'{"version":1,"attempt":1,"ended":true,"offset":0,"data":"","x":0}\n',
    b'{"version":1,"running":0,"queued":0}\n',
])
def test_a_logs_reply_of_another_shape_is_unexpected(rig, capsys, reply):
    fake = FakeWorker(rig.socket, reply)
    try:
        code = worker.logs(rig.socket, 1, None, False, timeout=5)
    finally:
        fake.close()
    assert code == 3
    assert capsys.readouterr() == ('', 'downloadinux: error: the worker is not answering: unexpected reply\n')


@pytest.mark.parametrize('argv', [['status'], ['logs', '1']])
def test_a_closed_standard_output_exits_1_without_a_traceback(rig, argv):
    prepare_attempts(rig, attempts=1)
    plant_log(rig, 1, 1, b'x' * 300000)  # more than a pipe holds
    serve = rig.start(env=rig.env(DL_VPN='on')).listening()
    read, write = os.pipe()
    os.close(read)  # the reader has gone
    try:
        process = rig.spawn(*argv, stdin=subprocess.DEVNULL, stdout=write)
    finally:
        os.close(write)
    code, _, err = rig.finish(process)
    assert (code, err) == (1, b'')
    assert serve.stop() == 0


# Standard-library boundary (§1, DEC-41, DEC-50, DEC-51).

BOUNDARY = r'''
import importlib.abc, sys
ALLOWED = ('downloadinux', 'downloadinux.worker', 'downloadinux.worker_execution',
           'downloadinux.worker_scheduler', 'downloadinux.nordvpn_output', 'downloadinux.job_store',
           'downloadinux.errors', 'downloadinux.cookie_store', 'downloadinux.cookiejar')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split('.')[0]
        if top not in sys.stdlib_module_names and top != 'downloadinux':
            raise ImportError('non-stdlib import: ' + name)
        if top == 'downloadinux' and name not in ALLOWED:
            raise ImportError('host import: ' + name)
sys.meta_path.insert(0, Block())
src, mode, sock = sys.argv[1:4]
sys.path.insert(0, src)
import io, json, os, signal, subprocess, time
from pathlib import Path
from downloadinux import worker
for blocked in ('downloadinux.launch', 'downloadinux.cookie_runner', 'downloadinux.settings', 'tomlkit'):
    try:
        __import__(blocked)
    except ImportError:
        pass
    else:
        raise AssertionError('import not blocked: ' + blocked)

def loaded():
    names = sorted(name for name in sys.modules if name.split('.')[0] == 'downloadinux')
    assert set(names) <= set(ALLOWED), names

if mode == 'serve':
    cookies, state, downloads = sys.argv[4:7]
    code = worker.run(['serve', '--cookies', cookies, '--state', state],
                      socket_path=Path(sock), downloads=Path(downloads))
    loaded()
    sys.exit(code)

class Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)

def client(argv, stdin=b''):
    out = io.TextIOWrapper(io.BytesIO(), encoding='ascii')
    saved = sys.stdout, sys.stdin
    sys.stdout, sys.stdin = out, Stdin(stdin)
    try:
        code = worker.run(argv, socket_path=Path(sock))
    finally:
        sys.stdout, sys.stdin = saved
    out.flush()
    return code, out.buffer.getvalue()

service = subprocess.Popen([sys.executable, '-I', __file__, src, 'serve', sock, *sys.argv[4:7]],
                           stdout=subprocess.PIPE)
deadline = time.monotonic() + 20
while not os.path.exists(sock):
    assert time.monotonic() < deadline and service.poll() is None
    time.sleep(0.02)
assert client(['status']) == (0, b'{"version":1,"running":0,"queued":0}\n')
request = dict(preset='video', lane='video', source=None, revision=None, user_agent='UA', urls=['exit=0'])
assert client(['submit'], json.dumps(request).encode()) == (
    0, b'{"version":1,"submission":1,"created":[{"id":1,"url":"exit=0"}],"duplicates":[]}\n')
while True:
    code, out = client(['show', '1'])
    assert code == 0 and time.monotonic() < deadline
    if json.loads(out)['job']['state'] == 'succeeded':
        break
    time.sleep(0.05)
service.send_signal(signal.SIGTERM)
assert service.wait(30) == 0
assert service.stdout.read().decode().splitlines() == [
    'downloadinux-worker: listening on ' + sock, 'downloadinux-worker: stopping']
loaded()
print('worker boundary passed')
'''


def test_isolated_stdlib_only_service_and_clients(rig):
    script = rig.tmp / 'boundary.py'
    script.write_text(BOUNDARY)
    result = subprocess.run(
        [sys.executable, '-I', str(script), str(SRC), 'client', str(rig.socket), str(rig.cookies),
         str(rig.state), str(rig.downloads)],
        capture_output=True, text=True, timeout=60, env=rig.env())
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'worker boundary passed\n'
    assert [r['url'] for r in rig.records()] == ['exit=0']
